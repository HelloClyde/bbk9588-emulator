"""QEMU system-emulation launcher for BBK 9588 raw MIPS images.

This module keeps QEMU orchestration separate from the web frontend. QEMU is a
process-level system emulator, so the integration point is a reproducible
command builder, process wrapper, and BBK/JZ47xx machine model launcher.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .ftl import normalize_c200_logical_tail_pages

DEFAULT_QEMU_EXECUTABLE = "qemu-system-mipsel"
DEFAULT_QEMU_MACHINE = "bbk9588"
DEFAULT_QEMU_CPU = "JZ4740"
DEFAULT_BBK9588_COMPAT_MACHINE_OPTIONS: tuple[str, ...] = ()
REMOVED_BBK9588_MACHINE_OPTIONS: tuple[str, ...] = (
    "semaphore-fastpath",
    "cache-scan-fastpath",
    "resource-release-fastpath",
)
DEFAULT_QEMU_BBK_INPUT_CHR_ID = "bbk9588-input"
DEFAULT_QEMU_BBK_FRAME_CHR_ID = "bbk9588-frame"
QEMU_BBK_FRAME_MAGIC = 0x464B4242
QEMU_BBK_PERF_MAGIC = 0x504B4242
QEMU_BBK_AUDIO_MAGIC = 0x414B4242
QEMU_BBK_FRAME_FORMAT_RGB565 = 0x00005635
QEMU_BBK_AUDIO_FORMAT_S16LE = 0x36314C53
QEMU_BBK_PERF_FORMAT_GUEST_INSNS = 0x00004950
QEMU_BBK_GUEST_SHUTDOWN_MARKER = "bbk9588: RTC HCR.PD requested guest shutdown"
QEMU_BBK_PERF_FORMAT_AIC = 0x00434941
QEMU_BBK_FRAME_HEADER = struct.Struct("<IIIIIII")
QEMU_BBK_PERF_PAYLOAD = struct.Struct("<QQ")
QEMU_BBK_AIC_PERF_PAYLOAD = struct.Struct("<24Q")
DEFAULT_C200_BASE = 0x80004000
DEFAULT_UBOOT_BASE = 0x80900000
DEFAULT_C200_PHYS = DEFAULT_C200_BASE & 0x1FFFFFFF
DEFAULT_UBOOT_PHYS = DEFAULT_UBOOT_BASE & 0x1FFFFFFF
DEFAULT_BOOTROM_LOAD_PHYS = 0x00000000
DEFAULT_BOOTROM_ENTRY = 0x80000004
DEFAULT_BOOTROM_NAND_PAGE = 0
DEFAULT_BOOTROM_LOAD_BYTES = 0x2000
DEFAULT_QEMU_DIAG_BASE = 0x89F00000
DEFAULT_QEMU_GUI_EVENT_SCRATCH = DEFAULT_QEMU_DIAG_BASE + 0x0000
DEFAULT_QEMU_TOUCH_TRACE = DEFAULT_QEMU_DIAG_BASE + 0x0100
QEMU_TOUCH_TRACE_MAGIC = 0x54434B42
DEFAULT_QEMU_DMAC_TRACE = DEFAULT_QEMU_DIAG_BASE + 0x0300
QEMU_DMAC_TRACE_MAGIC = 0x444D4B42
DEFAULT_QEMU_SURFACE_TRACE = DEFAULT_QEMU_DIAG_BASE + 0x0500
QEMU_SURFACE_TRACE_MAGIC = 0x53555246
DEFAULT_QEMU_NAND_IMAGE = Path("runtime") / "bbk9588_nand.bin"
DEFAULT_QEMU_NAND_IMAGE_CANDIDATES = (
    DEFAULT_QEMU_NAND_IMAGE,
    Path("build") / "bbk9588_nand_loader0_uboot40_fat_page1c40_root512_ftloob.bin",
    Path("build") / "bbk9588_nand_loader0_uboot40_fat_page1c40_root256_ftloob.bin",
    Path("build") / "bbk9588_nand_loader0_uboot40_fat_page1c40.bin",
    Path("build") / "bbk9588_nand_fat_page1c40_root512_ftloob.bin",
    Path("build") / "bbk9588_nand_fat_page1c40_root256_ftloob.bin",
    Path("build") / "bbk9588_nand_fat_page1c40.bin",
    Path("build") / "bbk9588_nand_uboot40_fat_page1c40_root512_ftloob.bin",
    Path("build") / "bbk9588_nand_uboot40_fat_page1c40_root256_ftloob.bin",
    Path("build") / "bbk9588_nand_uboot40_fat_page1c40.bin",
)
TOUCH_CALIBRATION_REFERENCE_POINTS = (
    (10, 10, 0x0E74, 0x0DDE),
    (230, 10, 0x0177, 0x0DDE),
    (230, 310, 0x0172, 0x00F0),
    (10, 310, 0x0E60, 0x00F0),
)


def _assign_windows_kill_on_close_job(
    proc: subprocess.Popen[str],
) -> tuple[object | None, str | None]:
    if os.name != "nt" or not hasattr(proc, "_handle"):
        return None, None

    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None, f"CreateJobObjectW failed: {ctypes.get_last_error()}"
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        return None, f"SetInformationJobObject failed: {error}"
    if not kernel32.AssignProcessToJobObject(
        handle, wintypes.HANDLE(proc._handle)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        return None, f"AssignProcessToJobObject failed: {error}"
    return handle, None


def _close_windows_job(handle: object | None) -> None:
    if handle is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


DEFAULT_QEMU_FIRMWARE_PATCHES = (
    "c200-lcd-ready",
    "c200-intc-no-pending",
    "c200-cp0-irq-enable-noop",
    "c200-cp0-status-restore-noop",
    "c200-graphics-done",
    "c200-touch-controller-ready",
    "c200-touch-gpio-latch",
    "c200-uart-ready",
    "c200-wait-noop",
    "c200-busy-delay-noop",
    "c200-no-event-poll-empty",
    "c200-event-loop-empty-safe",
)
BBK9588_C_DEVICE_FIRMWARE_PATCHES = DEFAULT_QEMU_FIRMWARE_PATCHES
DEFAULT_BBK9588_FIRMWARE_PATCHES: tuple[str, ...] = ()


KNOWN_FIRMWARE_PATCHES: dict[str, tuple[tuple[int, bytes], ...]] = {
    # Temporary QEMU-only device stub: C200 waits for LCD status bit 0x80 at
    # 0xb004300c. Malta does not model this BBK/JZ4740 register, so replace the
    # two early polling sequences with v0=0x80; nop; nop. This keeps the raw
    # source image untouched and lets QEMU discover the next missing device.
    "c200-lcd-ready": (
        (0x80012314, bytes.fromhex("80000234")),  # ori v0,zero,0x80
        (0x80012318, b"\x00\x00\x00\x00"),
        (0x8001231C, b"\x00\x00\x00\x00"),
        (0x80012338, bytes.fromhex("80000234")),  # ori v0,zero,0x80
        (0x8001233C, b"\x00\x00\x00\x00"),
        (0x80012340, b"\x00\x00\x00\x00"),
    ),
    # Temporary QEMU-only interrupt-controller stub. After LCD init, C200 asks
    # the INTC pending helper to merge a cached pending word with 0xb0001010.
    # Malta does not expose the BBK/JZ4740 INTC layout, so force both sources
    # to zero and let the helper return "no pending interrupt" (-1).
    "c200-intc-no-pending": (
        (0x80005320, bytes.fromhex("21100000")),  # addu v0,zero,zero
        (0x80005328, bytes.fromhex("21180000")),  # addu v1,zero,zero
    ),
    # Temporary QEMU-only CPU interrupt gate. C200's helper at 0x80005248
    # enables CP0 IE and clears EXL. On QEMU malta this admits board-level
    # timer/IP interrupts that do not correspond to the BBK/JZ4740 INTC model
    # yet, causing an exception-return loop before useful device work begins.
    "c200-cp0-irq-enable-noop": (
        (0x80005248, bytes.fromhex("0800e003")),  # jr ra
        (0x8000524C, b"\x00\x00\x00\x00"),
    ),
    # Temporary QEMU-only companion to c200-cp0-irq-enable-noop. These helpers
    # restore saved CP0 Status values that re-enable the Malta IP2 interrupt
    # line. NOP them so QEMU can progress to the next missing BBK/JZ device.
    "c200-cp0-status-restore-noop": (
        (0x800A7FA4, b"\x00\x00\x00\x00"),
        (0x800A80B4, b"\x00\x00\x00\x00"),
        (0x800A8134, b"\x00\x00\x00\x00"),
    ),
    # Temporary QEMU-only LCD/graphics status stub. C200 waits for
    # 0xb0021004 bit 0x800 after programming display registers; force the
    # ready value until the full graphics status path is modeled.
    "c200-graphics-done": (
        (0x8001005C, bytes.fromhex("00080234")),  # ori v0,zero,0x800
        (0x80010060, b"\x00\x00\x00\x00"),
    ),
    # Temporary QEMU-only touch/controller ready stub. The early calibration
    # gate polls 0xb0010200 bit 0x08000000; expose the ready bit so execution
    # reaches the next real device dependency.
    "c200-touch-controller-ready": (
        (0x80017BA4, bytes.fromhex("0008033c")),  # lui v1,0x0800
    ),
    # Temporary QEMU-only touch GPIO helper. The GUI/input path reads
    # 0xb0010100 bit 0x00040000 through 0x80059f68 as an active-low pen GPIO
    # mirrored from the touch latch. QEMU malta has no BBK GPIOC model, so
    # return the latch byte at 0x807f7110 directly.
    "c200-touch-gpio-latch": (
        (0x80059F68, bytes.fromhex("7f80023c")),  # lui v0,0x807f
        (0x80059F6C, bytes.fromhex("10714290")),  # lbu v0,0x7110(v0)
        (0x80059F70, bytes.fromhex("0800e003")),  # jr ra
        (0x80059F74, b"\x00\x00\x00\x00"),
    ),
    # Temporary QEMU-only UART status stub. C200's debug/printf path polls
    # BBK UART status at 0xb0030014 for TX ready bits 0x20/0x40. Malta does
    # not model that UART, so force those small helpers to observe ready.
    "c200-uart-ready": (
        (0x80005C9C, bytes.fromhex("20000234")),  # ori v0,zero,0x20
        (0x80005CD8, bytes.fromhex("40000234")),  # ori v0,zero,0x40
        (0x80005CDC, b"\x00\x00\x00\x00"),
        (0x80005D2C, bytes.fromhex("20000234")),  # ori v0,zero,0x20
        (0x80005D30, b"\x00\x00\x00\x00"),
    ),
    # Temporary QEMU-only low-power wait stub. These small paths mask selected
    # interrupt bits, execute MIPS wait, then restore the mask. Without the BBK
    # interrupt sources modeled in QEMU, wait can leave the frontend idle.
    "c200-wait-noop": (
        (0x8005BCD4, b"\x00\x00\x00\x00"),
        (0x8005BDE8, b"\x00\x00\x00\x00"),
    ),
    # 0x800043a0 is a calibrated software delay with no MMIO side effects.
    "c200-busy-delay-noop": (
        (0x800043A0, bytes.fromhex("0800e003")),  # jr ra
        (0x800043A4, b"\x00\x00\x00\x00"),
    ),
    # Early QEMU compatibility patch: return "no event" without walking the
    # full firmware event state machine on every pass.
    "c200-no-event-poll-empty": (
        (0x80058CB4, bytes.fromhex("21100000")),  # addu v0,zero,zero
        (0x80058CB8, bytes.fromhex("0800e003")),  # jr ra
        (0x80058CBC, b"\x00\x00\x00\x00"),
    ),
    # The event loop at 0x8012ccf4 can return the no-event sentinel in v0.
    # Stock firmware then unconditionally reads *(v0 + 4), which faults under
    # QEMU when the queue is empty.  Treat the empty case as event code 0 so
    # legacy Malta compatibility runs can keep progressing.  The bbk9588
    # default hardware path does not use this firmware patch.
    "c200-event-loop-empty-safe": (
        (0x8012CCFC, bytes.fromhex("21280000")),  # addu a1,zero,zero
    ),
}


KNOWN_STALL_REGIONS: tuple[tuple[int, int, str, str], ...] = (
    (
        0x80012310,
        0x80012344,
        "lcd-status-ready-wait",
        "C200 is polling LCD status register 0xb004300c bit 0x80; the bbk9588 LCD status model sets this from controller/frame activity without a machine ready override.",
    ),
    (
        0x800052B0,
        0x80005318,
        "tcu-init-path",
        "C200 is touching TCU registers around 0xb0002000/2004/2008/200c; the bbk9588 TCU model tracks timer registers and IRQ state.",
    ),
    (
        0x8000531C,
        0x80005394,
        "intc-pending-dispatch",
        "C200 is reading INTC pending/status at 0xb0001010 and dispatching interrupt bits.",
    ),
    (
        0x80004E64,
        0x80004EE0,
        "intc-mask-ack",
        "C200 is updating INTC mask/ack registers around 0xb0001008/1010.",
    ),
    (
        0x800099F0,
        0x80009A10,
        "c200-exception-return-loop",
        "C200 is returning through its exception/interrupt path; remaining IRQ work is in the bbk9588 INTC/CP0 wake path.",
    ),
    (
        0x80005200,
        0x80005218,
        "c200-irq-handler-return",
        "C200 has returned from a registered firmware IRQ handler and is restoring the interrupt wrapper state.",
    ),
    (
        0x8001AA04,
        0x8001AA20,
        "touch-irq-ack-return",
        "C200 is acknowledging the SADC/touch interrupt at INTC 0xb000100c and returning from the input IRQ path.",
    ),
    (
        0x80009840,
        0x80009868,
        "irq24-udc-service-loop",
        "C200 is enabling/clearing IRQ24 around 0xb000100c, waiting on the scheduler semaphore, and servicing the USB device controller.",
    ),
    (
        0x8000BA84,
        0x8000BB94,
        "c200-semaphore-wait",
        "C200 is executing its type-3 semaphore wait path. If count is zero, this enters scheduler/task blocking; correct progress depends on the modeled scheduler, timer, and IRQ state.",
    ),
    (
        0x8000BB98,
        0x8000BC54,
        "c200-semaphore-release",
        "C200 is executing its type-3 semaphore release path. This wakes blocked tasks through the firmware scheduler when waiters exist.",
    ),
    (
        0x800067F4,
        0x8000683C,
        "heap-free-with-semaphore",
        "C200 is freeing heap memory behind the heap lock at 0x80473f00; with the semaphore helper disabled this can enter the firmware scheduler wait path.",
    ),
    (
        0x80007648,
        0x80007698,
        "heap-alloc-with-semaphore",
        "C200 is allocating heap memory behind the heap lock at 0x80473f00; with the semaphore helper disabled this can enter the firmware scheduler wait path.",
    ),
    (
        0x800A7F80,
        0x800A7FE0,
        "c200-exception-vector-loop",
        "C200 is cycling through the exception vector path; check the bbk9588 INTC pending/mask and CP0 wake model.",
    ),
    (
        0x800A80F0,
        0x800A814C,
        "c200-cp0-status-helper",
        "C200 is in CP0 Status save/restore helpers; bbk9588 no longer uses Malta IRQs, so CP0 no-op patches are not default.",
    ),
    (
        0x80005278,
        0x80005290,
        "c200-cp0-status-clear-helper",
        "C200 is clearing CP0 Status IE around interrupt/critical-section handling.",
    ),
    (
        0x8001004C,
        0x80010068,
        "lcd-graphics-done-wait",
        "C200 is polling graphics/LCD status register 0xb0021004 bit 0x800; the bbk9588 graphics model now raises this from command completion without a machine ready override.",
    ),
    (
        0x80017B98,
        0x80017BE4,
        "touch-controller-ready-poll",
        "C200 is polling touch/controller register 0xb0010200 bit 0x08000000 during early calibration/input setup.",
    ),
    (
        0x80017BE8,
        0x80017CD0,
        "touch-calibration-loop",
        "C200 is in the early touch calibration loop; QEMU input/controller MMIO is not wired yet.",
    ),
    (
        0x8005A2D4,
        0x8005A340,
        "input-event-poll",
        "C200 is polling the input/event state machine; QEMU C touch/GPIO state exists, but full event injection still needs work.",
    ),
    (
        0x800DC588,
        0x800DC660,
        "gui-event-poller",
        "C200 is polling the display/event ring at 0x80825840; bbk9588 now exposes input through modeled SADC/GPIO/INTC and only mirrors host input events for diagnostics.",
    ),
    (
        0x8012BB90,
        0x8012BD10,
        "gui-tick-event-service",
        "C200 is in the GUI/tick event service path and is acknowledging TCU state around 0xb0002028.",
    ),
    (
        0x8012CCFC,
        0x8012CD04,
        "event-loop-empty-return",
        "C200 reached the event-loop empty return site; Python/GDB event synthesis is a legacy diagnostic path and is not part of the default bbk9588 hardware model.",
    ),
    (
        0x80059F68,
        0x80059F80,
        "touch-gpio-level-helper",
        "C200 is reading GPIO register 0xb0010100 bit 0x00040000 while processing touch/controller state.",
    ),
    (
        0x8005C350,
        0x8005C420,
        "touch-controller-mode-flag",
        "C200 is checking or toggling the firmware touch/controller mode flag at 0x8048daf4.",
    ),
    (
        0x80005C80,
        0x80005D90,
        "uart-status-wait",
        "C200 is polling BBK UART status register 0xb0030014 for TX/RX readiness; the bbk9588 UART model exposes 16550/JZ4740 line-status bits.",
    ),
    (
        0x8000E42C,
        0x8000E84C,
        "usb-udc-service",
        "C200 is servicing the BBK USB device controller window at 0xb3040000; the bbk9588 UDC model currently exposes the idle/no-host state.",
    ),
    (
        0x8005BBC0,
        0x8005BE10,
        "low-power-wait",
        "C200 is in a short interrupt-masked MIPS wait path; bbk9588 now handles the known WAIT instructions in QEMU C instead of using the c200-wait-noop firmware patch.",
    ),
    (
        0x80058CB4,
        0x80058DD4,
        "no-event-poll",
        "C200 is checking the firmware event queue; the no-event patch is no longer a bbk9588 default, but full event injection still needs work.",
    ),
    (
        0x800043A0,
        0x800043D8,
        "software-busy-delay",
        "C200 is burning cycles in the calibrated software delay loop; bbk9588 no longer skips this by default.",
    ),
    (
        0x80004A30,
        0x80004ABC,
        "c200-exception-report-tcu-restore",
        "C200 is in its exception reporting path and is restoring TCU registers around 0xb0002000; inspect CP0 EPC/BadVAddr for the original fault.",
    ),
    (
        0x800DE144,
        0x800DE150,
        "firmware-tick-getter",
        "C200 is reading the firmware tick counter used by event and input polling paths.",
    ),
    (
        0x801838FC,
        0x80183A60,
        "nand-scan-ready-marker-loop",
        "C200 is scanning NAND pages through the QEMU C CS0 data/cmd/addr windows; image-backed page reads are active and higher-level FTL/resource interpretation remains firmware-owned.",
    ),
    (
        0x801842F0,
        0x801843D0,
        "nand-oob-read-loop",
        "C200 is reading NAND OOB/marker data from the QEMU C NAND backing image while probing the flash layout.",
    ),
    (
        0x80183DD0,
        0x80183F88,
        "nand-bch-ecc-wait",
        "C200 is waiting for the BCH/ECC status register at 0xb3010114 after reading NAND data through the QEMU C CS0 window.",
    ),
    (
        0x8017CA10,
        0x8017CA80,
        "firmware-fat16-resource-cache-lookup",
        "C200 is looking up a 16-bit FAT entry through its firmware resource cache table; this is a diagnostic PC classification, not a QEMU storage/resource service.",
    ),
    (
        0x8017A928,
        0x8017A978,
        "resource-release-locked-wrapper",
        "C200 is taking the resource lock at 0x804bf43c before calling the resource object release routine; a bad a0 here usually means a file/cache record is being passed as a resource object.",
    ),
    (
        0x80170C70,
        0x80170D90,
        "resource-object-release",
        "C200 is releasing or cleaning a resource object; a fault at 0x80170c90 means the object pointer in a0 was invalid before reading field 0x48.",
    ),
    (
        0x80900DF0,
        0x80900E30,
        "uboot-tcu-clock-wait",
        "u-boot is in an early JZ4740 TCU/clock MMIO path around 0xb0002080 on stock QEMU malta.",
    ),
)


MIPS_EXCEPTION_CODES: dict[int, str] = {
    0: "interrupt",
    1: "tlb-modification",
    2: "tlb-load-or-ifetch",
    3: "tlb-store",
    4: "address-error-load-or-ifetch",
    5: "address-error-store",
    6: "bus-error-ifetch",
    7: "bus-error-load-store",
    8: "syscall",
    9: "breakpoint",
    10: "reserved-instruction",
    11: "coprocessor-unusable",
    12: "arithmetic-overflow",
    13: "trap",
    15: "floating-point",
}


@dataclass(frozen=True)
class QemuPayload:
    """A raw image to load into the QEMU guest address space."""

    path: Path
    load_addr: int
    force_raw: bool = True

    def loader_arg(self, set_pc: bool = False) -> str:
        qemu_path = str(self.path.resolve()).replace("\\", "/")
        pieces = [
            "loader",
            f"file={qemu_path}",
            f"addr=0x{self.load_addr:x}",
        ]
        if set_pc:
            pieces.append("cpu-num=0")
        if self.force_raw:
            pieces.append("force-raw=on")
        return ",".join(pieces)


@dataclass(frozen=True)
class QemuSystemConfig:
    """Configuration for a BBK 9588 QEMU system-emulation launch."""

    executable: str = DEFAULT_QEMU_EXECUTABLE
    machine: str = "malta"
    cpu: str = DEFAULT_QEMU_CPU
    ram_mb: int = 160
    accel: str = "tcg,thread=multi,tb-size=256"
    display: str = "none"
    serial: str = "mon:stdio"
    monitor: str = "none"
    gdb: str = "none"
    bbk_input: str = "none"
    bbk_frame: str = "none"
    bbk_machine_options: tuple[str, ...] = ()
    boot_payload: QemuPayload | None = None
    boot_load_addr: int = DEFAULT_C200_PHYS
    boot_pc: int = DEFAULT_C200_BASE
    nand_image: Path | None = None
    extra_payloads: tuple[QemuPayload, ...] = ()
    plugins: tuple[Path, ...] = ()
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float = 5.0
    firmware_patches: tuple[str, ...] = DEFAULT_QEMU_FIRMWARE_PATCHES
    startup_power_key: bool = False
    startup_power_hold_seconds: float = 0.75


@dataclass(frozen=True)
class QemuLaunchResult:
    """Result from a bounded QEMU launch."""

    command: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    elapsed_seconds: float
    stdout: str
    stderr: str
    hmp_samples: tuple[dict[str, object], ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "hmp_samples": list(self.hmp_samples),
        }


class QemuProcessBackend:
    """Long-lived QEMU process wrapper for the web frontend backend switch."""

    def __init__(self, config: QemuSystemConfig):
        self.config = config
        self.command: tuple[str, ...] = ()
        self.proc: subprocess.Popen[str] | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.stdout_tail: list[str] = []
        self.stderr_tail: list[str] = []
        self.stderr_log_path: Path | None = None
        self.returncode: int | None = None
        self.last_error: str | None = None
        self.stop_requested = False
        self.exit_reason: str | None = None
        self.hmp_port: int | None = None
        self.hmp_sock: socket.socket | None = None
        self.qmp_port: int | None = None
        self.qmp_sock: socket.socket | None = None
        self.qmp_last_event: dict[str, object] | None = None
        self.last_qmp_error: str | None = None
        self.gdb_port: int | None = None
        self.gdb_sock: socket.socket | None = None
        self.bbk_input_port: int | None = None
        self.bbk_input_sock: socket.socket | None = None
        self.bbk_frame_port: int | None = None
        self.bbk_frame_sock: socket.socket | None = None
        self.register_sample: dict[str, object] | None = None
        self.register_sample_at: float = 0.0
        self.memory_read_count = 0
        self.last_memory_read_error: str | None = None
        self.display_screendump_count = 0
        self.last_display_screendump_error: str | None = None
        self.frame_chardev_count = 0
        self.last_frame_chardev_error: str | None = None
        self.latest_frame_chardev: tuple[int, float, bytes] | None = None
        self.frame_ready_callback: Callable[[], None] | None = None
        self.latest_audio_chardev: tuple[int, float, int, int, bytes] | None = None
        self.audio_stream_packet_count = 0
        self.audio_stream_bytes = 0
        self.last_audio_stream_error: str | None = None
        self.audio_ready_callback: Callable[[int, int, int, bytes], None] | None = None
        self.performance_metrics: dict[str, object] = {}
        self.guest_insn_count: int | None = None
        self.guest_insn_count_at: float = 0.0
        self.guest_insn_count_qemu_ms: int | None = None
        self.guest_insn_ips: float | None = None
        self.guest_insn_packet_count = 0
        self.last_guest_insn_error: str | None = None
        self.audio_metrics: dict[str, object] = {}
        self.audio_packet_count = 0
        self._perf_last_time: float | None = None
        self._perf_last_frame_chardev_count = 0
        self._perf_last_process_cpu_seconds: float | None = None
        self.gdb_read_count = 0
        self.gdb_write_count = 0
        self.gdb_register_read_count = 0
        self.gdb_register_write_count = 0
        self.gdb_step_count = 0
        self.guest_call_count = 0
        self.bbk_input_write_count = 0
        self.last_bbk_input_error: str | None = None
        self.usb_power_connected = self._bbk_machine_bool_option_value(
            "usb-power-connected", True
        )
        self.last_gdb_error: str | None = None
        self.guest_input_events: list[dict[str, object]] = []
        self.touch_capture_active: int | None = None
        self.legacy_python_storage_hook_events: list[dict[str, object]] = []
        self.legacy_python_storage_hook_count = 0
        self.event_loop_empty_fix_count = 0
        self.event_loop_synth_event_count = 0
        self.task_context_events: list[dict[str, object]] = []
        self.task_context_trace_count = 0
        self.gui_timer_events: list[dict[str, object]] = []
        self.gui_timer_tick_count = 0
        self.gui_timer_fire_count = 0
        self.fs_trace_events: list[dict[str, object]] = []
        self.fs_trace_count = 0
        self.event_loop_trace_events: list[dict[str, object]] = []
        self.event_loop_trace_count = 0
        self.resource_trace_events: list[dict[str, object]] = []
        self.resource_trace_count = 0
        # Diagnostic-only backing image caches; not part of the bbk9588 hardware path.
        self.diagnostic_fat16_layout_cache: dict[str, int] | None = None
        self.diagnostic_fat16_long_name_alias_cache: dict[bytes, list[bytes]] | None = None
        self.diagnostic_nand_fat_sector0_cache: int | None = None
        self.diagnostic_backing_sector_cache: dict[int, bytes] = {}
        self.qemu_heap_next = 0x80960000
        self._lock = threading.RLock()
        self._last_snapshot: dict[str, object] = {}
        self._reader_threads: list[threading.Thread] = []
        self._qmp_reader_thread: threading.Thread | None = None
        self._frame_reader_thread: threading.Thread | None = None
        self._process_job_handle: object | None = None
        self.last_process_job_error: str | None = None

    def _bbk_machine_bool_option_enabled(self, name: str) -> bool:
        return self._bbk_machine_bool_option_value(name, False)

    def _bbk_machine_bool_option_value(self, name: str, default: bool) -> bool:
        needle = name.lower()
        for option in self.config.bbk_machine_options:
            key, sep, value = str(option).partition("=")
            if key.lower() != needle:
                continue
            if not sep:
                return True
            return value.strip().lower() in {"1", "on", "true", "yes"}
        return bool(default)

    def _update_performance_metrics_locked(
        self,
        now: float,
        elapsed: float | None,
    ) -> dict[str, object]:
        proc = self.proc
        running = proc is not None and proc.poll() is None
        cpu_seconds = _process_cpu_time_seconds(proc.pid) if running and proc is not None else None
        host_cpus = os.cpu_count() or 1
        frame_count = int(self.frame_chardev_count)
        interval = (
            max(0.0, now - self._perf_last_time)
            if self._perf_last_time is not None
            else None
        )
        frame_fps: float | None = None
        cpu_one_core_percent: float | None = None
        cpu_host_percent: float | None = None
        if interval is not None and interval > 0:
            frame_delta = max(0, frame_count - self._perf_last_frame_chardev_count)
            frame_fps = frame_delta / interval
            if cpu_seconds is not None and self._perf_last_process_cpu_seconds is not None:
                cpu_delta = max(0.0, cpu_seconds - self._perf_last_process_cpu_seconds)
                cpu_one_core_percent = (cpu_delta / interval) * 100.0
                cpu_host_percent = cpu_one_core_percent / host_cpus

        average_fps = (
            frame_count / elapsed
            if isinstance(elapsed, (int, float)) and elapsed > 0
            else None
        )
        metrics: dict[str, object] = {
            "sampled_at": now,
            "sample_interval_seconds": None if interval is None else round(interval, 3),
            "frame_chardev_fps": None if frame_fps is None else round(frame_fps, 2),
            "frame_chardev_average_fps": None if average_fps is None else round(average_fps, 2),
            "qemu_cpu_time_seconds": None if cpu_seconds is None else round(cpu_seconds, 3),
            "qemu_cpu_one_core_percent": (
                None if cpu_one_core_percent is None else round(cpu_one_core_percent, 1)
            ),
            "qemu_cpu_host_percent": None if cpu_host_percent is None else round(cpu_host_percent, 1),
            "host_logical_cpus": host_cpus,
            "guest_ips": None if self.guest_insn_ips is None else round(self.guest_insn_ips, 1),
            "guest_ips_available": self.guest_insn_ips is not None and (
                self.guest_insn_count_at <= 0.0 or now - self.guest_insn_count_at <= 3.0
            ),
            "guest_ips_source": (
                "bbk9588-frame-chardev"
                if self.guest_insn_count is not None
                else "waiting for bbk9588 frame-chardev performance packet"
            ),
            "guest_insn_count": self.guest_insn_count,
            "guest_insn_count_at": self.guest_insn_count_at or None,
            "guest_insn_count_qemu_ms": self.guest_insn_count_qemu_ms,
            "guest_insn_packet_count": self.guest_insn_packet_count,
            "audio": dict(self.audio_metrics),
            "audio_packet_count": self.audio_packet_count,
        }
        self.performance_metrics = metrics
        self._perf_last_time = now
        self._perf_last_frame_chardev_count = frame_count
        self._perf_last_process_cpu_seconds = cpu_seconds
        return metrics

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            launch_config = self.config
            if self.config.monitor == "none":
                self.hmp_port = _find_free_tcp_port()
                launch_config = replace(
                    self.config,
                    monitor=f"tcp:127.0.0.1:{self.hmp_port},server,nowait",
                    serial="none" if self.config.serial == "mon:stdio" else self.config.serial,
                )
            if launch_config.gdb == "auto":
                self.gdb_port = _find_free_tcp_port()
                launch_config = replace(launch_config, gdb=f"tcp:127.0.0.1:{self.gdb_port}")
            if not any(str(arg).strip().lower() == "-qmp" for arg in launch_config.extra_args):
                self.qmp_port = _find_free_tcp_port()
                launch_config = replace(
                    launch_config,
                    extra_args=(
                        *launch_config.extra_args,
                        "-qmp",
                        f"tcp:127.0.0.1:{self.qmp_port},server=on,wait=off",
                    ),
                )
            if launch_config.machine.lower() == "bbk9588" and launch_config.bbk_input == "none":
                self.bbk_input_port = _find_free_tcp_port()
                launch_config = replace(
                    launch_config,
                    bbk_input=(
                        f"socket,id={DEFAULT_QEMU_BBK_INPUT_CHR_ID},"
                        f"host=127.0.0.1,port={self.bbk_input_port},"
                        "server=on,wait=off,nodelay=on"
                    ),
                )
            if launch_config.machine.lower() == "bbk9588" and launch_config.bbk_frame == "none":
                self.bbk_frame_port = _find_free_tcp_port()
                launch_config = replace(
                    launch_config,
                    bbk_frame=(
                        f"socket,id={DEFAULT_QEMU_BBK_FRAME_CHR_ID},"
                        f"host=127.0.0.1,port={self.bbk_frame_port},"
                        "server=on,wait=off,nodelay=on"
                    ),
                )
            if (
                launch_config.machine.lower() == "bbk9588"
                and self.hmp_port is not None
                and not any(str(arg).strip().lower() == "-s" for arg in launch_config.extra_args)
            ):
                # Keep the guest behind a startup barrier until its input and frame
                # chardev clients are attached, otherwise a fast boot can lose the
                # only LCD update before the web frontend starts reading frames.
                launch_config = replace(
                    launch_config,
                    extra_args=(*launch_config.extra_args, "-S"),
                )
            raw_command = build_qemu_command(launch_config)
            resolved = find_qemu(raw_command[0])
            if resolved is None:
                raise FileNotFoundError(f"could not find {raw_command[0]!r}")
            raw_command[0] = resolved
            self.command = tuple(raw_command)
            self.stdout_tail = []
            self.stderr_tail = []
            log_dir = Path("build")
            log_dir.mkdir(parents=True, exist_ok=True)
            self.stderr_log_path = log_dir / f"qemu_stderr_{int(time.time() * 1000)}.log"
            self.returncode = None
            self.last_error = None
            self.stop_requested = False
            self.exit_reason = None
            self.qmp_last_event = None
            self.last_qmp_error = None
            self.register_sample = None
            self.register_sample_at = 0.0
            self.memory_read_count = 0
            self.last_memory_read_error = None
            self.display_screendump_count = 0
            self.last_display_screendump_error = None
            self.frame_chardev_count = 0
            self.last_frame_chardev_error = None
            self.latest_frame_chardev = None
            self.latest_audio_chardev = None
            self.audio_stream_packet_count = 0
            self.audio_stream_bytes = 0
            self.last_audio_stream_error = None
            self.performance_metrics = {}
            self.guest_insn_count = None
            self.guest_insn_count_at = 0.0
            self.guest_insn_count_qemu_ms = None
            self.guest_insn_ips = None
            self.guest_insn_packet_count = 0
            self.last_guest_insn_error = None
            self.audio_metrics = {}
            self.audio_packet_count = 0
            self._perf_last_time = None
            self._perf_last_frame_chardev_count = 0
            self._perf_last_process_cpu_seconds = None
            self.gdb_read_count = 0
            self.gdb_write_count = 0
            self.gdb_register_read_count = 0
            self.gdb_register_write_count = 0
            self.gdb_step_count = 0
            self.guest_call_count = 0
            self.bbk_input_write_count = 0
            self.last_bbk_input_error = None
            self.last_gdb_error = None
            self.guest_input_events = []
            self.legacy_python_storage_hook_events = []
            self.legacy_python_storage_hook_count = 0
            self.event_loop_empty_fix_count = 0
            self.event_loop_synth_event_count = 0
            self.task_context_events = []
            self.task_context_trace_count = 0
            self.gui_timer_events = []
            self.gui_timer_tick_count = 0
            self.gui_timer_fire_count = 0
            self.fs_trace_events = []
            self.fs_trace_count = 0
            self.event_loop_trace_events = []
            self.event_loop_trace_count = 0
            self.resource_trace_events = []
            self.resource_trace_count = 0
            self.diagnostic_fat16_layout_cache = None
            self.diagnostic_fat16_long_name_alias_cache = None
            self.diagnostic_nand_fat_sector0_cache = None
            self.diagnostic_backing_sector_cache = {}
            self.qemu_heap_next = 0x80960000
            self.finished_at = None
            self.started_at = time.time()
            _close_windows_job(self._process_job_handle)
            self._process_job_handle = None
            self.last_process_job_error = None
            self.proc = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=qemu_subprocess_env(self.command[0]),
                creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0),
            )
            (
                self._process_job_handle,
                self.last_process_job_error,
            ) = _assign_windows_kill_on_close_job(self.proc)
            self._reader_threads = [
                threading.Thread(target=self._reader, args=("stdout", self.proc.stdout), daemon=True),
                threading.Thread(target=self._reader, args=("stderr", self.proc.stderr), daemon=True),
            ]
            for thread in self._reader_threads:
                thread.start()
            if self.qmp_port is not None:
                try:
                    self.qmp_sock = _connect_qmp(self.qmp_port, timeout=1.5)
                    self._qmp_reader_thread = threading.Thread(
                        target=self._qmp_reader,
                        name="bbk9588-qmp-events",
                        daemon=True,
                    )
                    self._qmp_reader_thread.start()
                except Exception as exc:
                    self.qmp_sock = None
                    self.last_qmp_error = f"{type(exc).__name__}: {exc}"
            if self.hmp_port is not None:
                try:
                    self.hmp_sock = _connect_hmp(self.hmp_port, timeout=1.5)
                except Exception as exc:
                    self.hmp_sock = None
                    self.last_error = f"HMP {type(exc).__name__}: {exc}"
            if self.gdb_port is not None:
                try:
                    self.gdb_sock = _connect_gdb(self.gdb_port, timeout=1.5)
                except Exception as exc:
                    if self.gdb_sock is not None:
                        try:
                            self.gdb_sock.close()
                        except OSError:
                            pass
                    self.gdb_sock = None
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"
            if self.bbk_input_port is not None:
                try:
                    self.bbk_input_sock = _connect_bbk_input(self.bbk_input_port, timeout=1.5)
                except Exception as exc:
                    self.bbk_input_sock = None
                    self.last_bbk_input_error = f"{type(exc).__name__}: {exc}"
            if self.bbk_frame_port is not None:
                try:
                    self.bbk_frame_sock = _connect_bbk_input(self.bbk_frame_port, timeout=1.5)
                    self.bbk_frame_sock.settimeout(None)
                    self._frame_reader_thread = threading.Thread(target=self._frame_reader, daemon=True)
                    self._frame_reader_thread.start()
                except Exception as exc:
                    self.bbk_frame_sock = None
                    self.last_frame_chardev_error = f"{type(exc).__name__}: {exc}"
            startup_power_pressed = False
            if self.config.startup_power_key:
                result = self.apply_gui_key_event(11, True)
                startup_power_pressed = bool(result.get("applied"))
                if not startup_power_pressed:
                    self.last_bbk_input_error = str(
                        result.get("error") or "startup power key was not accepted"
                    )
            try:
                hmp_status = (
                    _hmp_command(self.hmp_sock, "info status")
                    if self.hmp_sock is not None
                    else ""
                )
                if "paused" in hmp_status.lower() or (
                    not hmp_status and self.gdb_sock is not None
                ):
                    if self.gdb_sock is not None:
                        stop_reply = _gdb_packet(self.gdb_sock, "?")
                        if not stop_reply.startswith(("S", "T", "W", "X")):
                            raise RuntimeError(
                                f"unexpected initial GDB stop reply: {stop_reply!r}"
                            )
                        _gdb_continue(self.gdb_sock)
                    elif self.hmp_sock is not None:
                        _hmp_command(self.hmp_sock, "cont")
            except Exception as exc:
                self.last_gdb_error = f"initial resume {type(exc).__name__}: {exc}"
                if self.gdb_sock is not None:
                    try:
                        self.gdb_sock.close()
                    except OSError:
                        pass
                    self.gdb_sock = None
                if self.hmp_sock is not None:
                    try:
                        _hmp_command(self.hmp_sock, "cont")
                    except Exception as hmp_exc:
                        self.last_error = f"HMP resume {type(hmp_exc).__name__}: {hmp_exc}"
            if startup_power_pressed:
                threading.Thread(
                    target=self._release_startup_power_key,
                    name="bbk9588-startup-power-key",
                    daemon=True,
                ).start()

    def _release_startup_power_key(self) -> None:
        deadline = time.monotonic() + max(
            0.0, float(self.config.startup_power_hold_seconds)
        )
        while time.monotonic() < deadline:
            with self._lock:
                if self.proc is None or self.proc.poll() is not None:
                    return
                if self.latest_frame_chardev is not None:
                    break
            time.sleep(0.05)
        self.apply_gui_key_event(11, False)

    def _reader(self, name: str, stream) -> None:
        if stream is None:
            return
        tail = self.stdout_tail if name == "stdout" else self.stderr_tail
        log_path = self.stderr_log_path if name == "stderr" else None
        try:
            for line in stream:
                if log_path is not None:
                    try:
                        with log_path.open("a", encoding="utf-8", errors="replace") as f:
                            f.write(line)
                    except OSError:
                        pass
                with self._lock:
                    text = line.rstrip("\r\n")
                    tail.append(text)
                    del tail[:-200]
                    if (
                        name == "stderr"
                        and QEMU_BBK_GUEST_SHUTDOWN_MARKER in text
                        and not self.stop_requested
                    ):
                        self.exit_reason = "guest-shutdown"
        except Exception as exc:
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {exc}"

    def _handle_qmp_message_locked(self, message: dict[str, object]) -> None:
        event = message.get("event")
        if not isinstance(event, str):
            return
        data = message.get("data")
        event_data = data if isinstance(data, dict) else {}
        self.qmp_last_event = {
            "event": event,
            "data": dict(event_data),
            "timestamp": message.get("timestamp"),
        }
        if event != "SHUTDOWN" or self.stop_requested:
            return
        reason = event_data.get("reason")
        self.exit_reason = str(reason) if reason else "process-exit"

    def _qmp_reader(self) -> None:
        sock = self.qmp_sock
        if sock is None:
            return
        buffer = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    message = json.loads(line.decode("utf-8"))
                    if isinstance(message, dict):
                        with self._lock:
                            self._handle_qmp_message_locked(message)
        except (OSError, ValueError) as exc:
            with self._lock:
                if self.proc is not None and self.proc.poll() is None:
                    self.last_qmp_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                if self.qmp_sock is sock:
                    self.qmp_sock = None

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = int(size)
        while remaining > 0:
            try:
                chunk = sock.recv(remaining)
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError("frame chardev closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _record_guest_insn_count_locked(
        self,
        guest_count: int,
        qemu_ms: int,
        now: float,
    ) -> None:
        previous_count = self.guest_insn_count
        previous_at = self.guest_insn_count_at
        if previous_count is not None and previous_at > 0.0 and now > previous_at:
            delta = max(0, int(guest_count) - int(previous_count))
            self.guest_insn_ips = delta / (now - previous_at)
        self.guest_insn_count = int(guest_count)
        self.guest_insn_count_at = now
        self.guest_insn_count_qemu_ms = int(qemu_ms)
        self.guest_insn_packet_count += 1
        self.last_guest_insn_error = None

    def _record_audio_metrics_locked(
        self,
        values: tuple[int, ...],
        now: float,
    ) -> None:
        (
            sample_rate,
            tx_fifo_level,
            rx_fifo_level,
            flags,
            aicfr,
            aiccr,
            cdccr1,
            cdccr2,
            tx_dma_samples,
            rx_dma_samples,
            output_frames,
            input_frames,
            underruns,
            overruns,
            dma_completion_count,
            dma_rearm_count,
            dma_last_rearm_gap_ns,
            dma_max_rearm_gap_ns,
            dma_total_rearm_gap_ns,
            dma_last_gap_underruns,
            dma_total_gap_underruns,
            dma_last_units,
            dma_completion_fifo,
            dma_rearm_fifo,
        ) = values
        self.audio_packet_count += 1
        self.audio_metrics = {
            "sample_rate_hz": sample_rate,
            "tx_fifo_level": tx_fifo_level,
            "rx_fifo_level": rx_fifo_level,
            "flags": flags,
            "playing": bool(flags & 0x01),
            "recording": bool(flags & 0x02),
            "muted": bool(flags & 0x04),
            "timer_running": bool(flags & 0x08),
            "output_voice": bool(flags & 0x10),
            "input_voice": bool(flags & 0x20),
            "aicfr": f"0x{aicfr & 0xFFFFFFFF:08x}",
            "aiccr": f"0x{aiccr & 0xFFFFFFFF:08x}",
            "cdccr1": f"0x{cdccr1 & 0xFFFFFFFF:08x}",
            "cdccr2": f"0x{cdccr2 & 0xFFFFFFFF:08x}",
            "tx_dma_samples": tx_dma_samples,
            "rx_dma_samples": rx_dma_samples,
            "output_frames": output_frames,
            "input_frames": input_frames,
            "underruns": underruns,
            "overruns": overruns,
            "dma_completion_count": dma_completion_count,
            "dma_rearm_count": dma_rearm_count,
            "dma_last_rearm_gap_ns": dma_last_rearm_gap_ns,
            "dma_max_rearm_gap_ns": dma_max_rearm_gap_ns,
            "dma_total_rearm_gap_ns": dma_total_rearm_gap_ns,
            "dma_last_gap_underruns": dma_last_gap_underruns,
            "dma_total_gap_underruns": dma_total_gap_underruns,
            "dma_last_units": dma_last_units,
            "dma_completion_fifo": dma_completion_fifo,
            "dma_rearm_fifo": dma_rearm_fifo,
            "packet_count": self.audio_packet_count,
            "updated_at": now,
        }

    def set_frame_ready_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self.frame_ready_callback = callback

    def set_audio_ready_callback(
        self,
        callback: Callable[[int, int, int, bytes], None] | None,
    ) -> None:
        with self._lock:
            self.audio_ready_callback = callback

    def _frame_reader(self) -> None:
        sock = self.bbk_frame_sock
        if sock is None:
            return
        try:
            while True:
                header = self._recv_exact(sock, QEMU_BBK_FRAME_HEADER.size)
                magic, seq, width, height, stride, fmt, payload_len = QEMU_BBK_FRAME_HEADER.unpack(header)
                if magic == QEMU_BBK_AUDIO_MAGIC:
                    sample_rate = width
                    channels = height
                    if (
                        sample_rate < 8000
                        or sample_rate > 192000
                        or channels not in (1, 2)
                        or stride != channels * 2
                        or fmt != QEMU_BBK_AUDIO_FORMAT_S16LE
                        or payload_len == 0
                        or payload_len > 256 * 1024
                        or payload_len % stride != 0
                    ):
                        raise ValueError(
                            "invalid audio chardev header "
                            f"seq={seq} rate={sample_rate} channels={channels} "
                            f"stride={stride} fmt=0x{fmt:08x} len={payload_len}"
                        )
                    payload = self._recv_exact(sock, payload_len)
                    captured_at = time.time()
                    with self._lock:
                        self.latest_audio_chardev = (
                            seq,
                            captured_at,
                            sample_rate,
                            channels,
                            payload,
                        )
                        self.audio_stream_packet_count += 1
                        self.audio_stream_bytes += payload_len
                        self.last_audio_stream_error = None
                        audio_ready_callback = self.audio_ready_callback
                    if audio_ready_callback is not None:
                        try:
                            audio_ready_callback(seq, sample_rate, channels, payload)
                        except Exception:
                            # Browser delivery must never stop chardev ingestion.
                            pass
                    continue
                if magic == QEMU_BBK_PERF_MAGIC:
                    if width != 1:
                        raise ValueError(
                            "invalid perf chardev header "
                            f"seq={seq} version={width} fmt=0x{fmt:08x} "
                            f"len={payload_len}"
                        )
                    if (
                        fmt == QEMU_BBK_PERF_FORMAT_GUEST_INSNS
                        and payload_len == QEMU_BBK_PERF_PAYLOAD.size
                    ):
                        payload = self._recv_exact(sock, payload_len)
                        guest_count, qemu_ms = QEMU_BBK_PERF_PAYLOAD.unpack(payload)
                        with self._lock:
                            self._record_guest_insn_count_locked(guest_count, qemu_ms, time.time())
                    elif (
                        fmt == QEMU_BBK_PERF_FORMAT_AIC
                        and payload_len == QEMU_BBK_AIC_PERF_PAYLOAD.size
                    ):
                        payload = self._recv_exact(sock, payload_len)
                        values = QEMU_BBK_AIC_PERF_PAYLOAD.unpack(payload)
                        with self._lock:
                            self._record_audio_metrics_locked(values, time.time())
                    else:
                        raise ValueError(
                            "invalid perf chardev payload "
                            f"seq={seq} fmt=0x{fmt:08x} len={payload_len}"
                        )
                    continue
                if (
                    magic != QEMU_BBK_FRAME_MAGIC
                    or width != 240
                    or height != 320
                    or stride != 480
                    or fmt != QEMU_BBK_FRAME_FORMAT_RGB565
                    or payload_len != 240 * 320 * 2
                ):
                    raise ValueError(
                        "invalid frame chardev header "
                        f"magic=0x{magic:08x} {width}x{height} stride={stride} "
                        f"fmt=0x{fmt:08x} len={payload_len}"
                    )
                payload = self._recv_exact(sock, payload_len)
                captured_at = time.time()
                with self._lock:
                    self.latest_frame_chardev = (seq, captured_at, payload)
                    self.frame_chardev_count += 1
                    self.last_frame_chardev_error = None
                    frame_ready_callback = self.frame_ready_callback
                if frame_ready_callback is not None:
                    try:
                        frame_ready_callback()
                    except Exception:
                        # A frontend notification must never stop frame ingestion.
                        pass
        except Exception as exc:
            with self._lock:
                self.last_frame_chardev_error = f"{type(exc).__name__}: {exc}"
                if "audio" in str(exc).lower():
                    self.last_audio_stream_error = self.last_frame_chardev_error
                if "perf" in str(exc).lower():
                    self.last_guest_insn_error = self.last_frame_chardev_error

    def stop(self, timeout: float = 2.0) -> None:
        quit_sent = False
        with self._lock:
            proc = self.proc
            if proc is not None and proc.poll() is None:
                self.stop_requested = True
            if (
                proc is not None
                and proc.poll() is None
                and self.hmp_sock is not None
            ):
                try:
                    _hmp_command(self.hmp_sock, "quit")
                    quit_sent = True
                except (ConnectionResetError, BrokenPipeError, EOFError):
                    # QEMU may close the monitor before returning a prompt.
                    quit_sent = True
                except Exception as exc:
                    self.last_error = f"HMP quit {type(exc).__name__}: {exc}"
        if proc is None:
            return
        if proc.poll() is None:
            if not quit_sent:
                proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if quit_sent:
                    proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=timeout)
        with self._lock:
            self._record_process_exit_locked(proc.returncode)

    def _record_process_exit_locked(self, returncode: int | None) -> None:
        if returncode is None:
            return
        self.returncode = int(returncode)
        if self.finished_at is None:
            self.finished_at = time.time()
        if self.exit_reason is None:
            if self.stop_requested:
                self.exit_reason = "user-stop"
            elif self.returncode != 0:
                self.exit_reason = "process-error"
            else:
                self.exit_reason = "process-exit"
        # QMP must drain the final SHUTDOWN event before its reader sees EOF.
        for name in ("hmp_sock", "gdb_sock", "bbk_input_sock", "bbk_frame_sock"):
            sock = getattr(self, name)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, name, None)
        _close_windows_job(self._process_job_handle)
        self._process_job_handle = None

    def set_storage_trace(self, enabled: bool) -> dict[str, object]:
        """Enable or disable the bbk9588 storage trace at runtime."""

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            if self.hmp_sock is None:
                return {"error": "QEMU HMP monitor is not connected"}
            value = "true" if enabled else "false"
            try:
                _hmp_command(
                    self.hmp_sock,
                    f"qom-set /machine storage-trace {value}",
                )
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            return {
                "storage_trace_enabled": bool(enabled),
            }

    def apply_usb_power_state(self, connected: bool) -> dict[str, object]:
        """Drive the BBK9588 board-level USB/charger power-detect input."""

        connected = bool(connected)
        with self._lock:
            if self.config.machine.lower() != "bbk9588":
                return {
                    "applied": False,
                    "error": "USB power state requires the bbk9588 machine",
                }
            if self.proc is None or self.proc.poll() is not None:
                return {"applied": False, "error": "QEMU process is not running"}
            if self.hmp_sock is None:
                return {
                    "applied": False,
                    "error": "QEMU HMP monitor is not connected",
                }
            value = "true" if connected else "false"
            try:
                _hmp_command(
                    self.hmp_sock,
                    f"qom-set /machine usb-power-connected {value}",
                )
            except Exception as exc:
                return {"applied": False, "error": f"{type(exc).__name__}: {exc}"}
            self.usb_power_connected = connected
            return {
                "applied": True,
                "connected": connected,
                "source": "qemu-machine-property",
            }

    def refresh(self) -> None:
        with self._lock:
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                self._record_process_exit_locked(proc.returncode)
            elif proc is not None and self.hmp_sock is not None and time.time() - self.register_sample_at >= 0.5:
                try:
                    status = _hmp_command(self.hmp_sock, "info status")
                    if "VM status: paused" in status or "VM status: stopped" in status:
                        _hmp_command(self.hmp_sock, "cont")
                        time.sleep(0.02)
                    raw = _hmp_command(self.hmp_sock, "info registers")
                    self.register_sample = _parse_register_sample(raw, time.time() - (self.started_at or time.time()))
                    self.register_sample_at = time.time()
                    if self.last_error and self.last_error.startswith("HMP "):
                        self.last_error = None
                except Exception as exc:
                    self.last_error = f"HMP {type(exc).__name__}: {exc}"

    def running(self) -> bool:
        self.refresh()
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def _send_bbk_input_locked(self, line: str) -> bool:
        if self.bbk_input_sock is None:
            return False
        try:
            self.bbk_input_sock.sendall((line.rstrip("\r\n") + "\n").encode("ascii"))
            self.bbk_input_write_count += 1
            self.last_bbk_input_error = None
            return True
        except Exception as exc:
            self.last_bbk_input_error = f"{type(exc).__name__}: {exc}"
            try:
                self.bbk_input_sock.close()
            except OSError:
                pass
            self.bbk_input_sock = None
            return False

    def read_physical_memory(self, addr: int, size: int) -> bytes:
        if size <= 0:
            return b""
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.hmp_sock is None:
                raise RuntimeError("QEMU HMP monitor is not connected")
            out_dir = Path("build") / "qemu_mem"
            out_dir.mkdir(parents=True, exist_ok=True)
            pid = self.proc.pid
            out_path = (out_dir / f"pmem_{pid}_{addr & 0xFFFFFFFF:x}_{size:x}_{self.memory_read_count}.bin").resolve()
            if out_path.exists():
                out_path.unlink()
            command = f"pmemsave 0x{addr & 0xFFFFFFFF:x} {int(size)} {str(out_path).replace(chr(92), '/')}"
            try:
                _hmp_command(self.hmp_sock, command)
                data = out_path.read_bytes()
                if len(data) != size:
                    raise IOError(f"pmemsave returned {len(data)} bytes, expected {size}")
                self.memory_read_count += 1
                self.last_memory_read_error = None
                return data
            except Exception as exc:
                self.last_memory_read_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    out_path.unlink()
                except OSError:
                    pass

    def read_display_rgb565_frame(self) -> tuple[bytes, str]:
        """Read the QEMU graphic console as a 240x320 RGB565 frame."""

        deadline = time.time() + 1.0
        while time.time() < deadline:
            with self._lock:
                latest = self.latest_frame_chardev
                if latest is not None:
                    return latest[2], "qemu-frame-chardev"
                if self.proc is None or self.proc.poll() is not None:
                    raise RuntimeError("QEMU process is not running")
            time.sleep(0.02)

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.hmp_sock is None:
                raise RuntimeError("QEMU HMP monitor is not connected")
            out_dir = Path("build") / "qemu_display"
            out_dir.mkdir(parents=True, exist_ok=True)
            pid = self.proc.pid
            out_path = (out_dir / f"screendump_{pid}_{self.display_screendump_count}.ppm").resolve()
            if out_path.exists():
                out_path.unlink()
            command = f"screendump {str(out_path).replace(chr(92), '/')}"
            try:
                _hmp_command(self.hmp_sock, command)
                deadline = time.time() + 1.0
                last_size = -1
                while time.time() < deadline:
                    if out_path.exists():
                        size = out_path.stat().st_size
                        if size > 0 and size == last_size:
                            break
                        last_size = size
                    time.sleep(0.03)
                data = out_path.read_bytes()
                raw = _ppm_to_rgb565(data)
                self.display_screendump_count += 1
                self.last_display_screendump_error = None
                return raw, "hmp-screendump"
            except Exception as exc:
                self.last_display_screendump_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    out_path.unlink()
                except OSError:
                    pass

    def read_virtual_memory(self, addr: int, size: int) -> bytes:
        """Read guest virtual memory through the QEMU GDB stub."""

        if size <= 0:
            return b""
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            try:
                self._pause_for_gdb_locked()
                data = self._read_virtual_memory_paused_locked(addr, size)
                self.gdb_read_count += 1
                self.last_gdb_error = None
                return data
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def _read_virtual_memory_paused_locked(self, addr: int, size: int) -> bytes:
        if self.gdb_sock is None:
            raise RuntimeError("QEMU GDB stub is not connected")
        payload = _gdb_packet(self.gdb_sock, f"m{addr & 0xFFFFFFFF:x},{int(size):x}")
        if payload.startswith("E"):
            raise IOError(f"GDB read failed: {payload}")
        data = bytes.fromhex(payload)
        if len(data) != size:
            raise IOError(f"GDB read returned {len(data)} bytes, expected {size}")
        return data

    def _read_guest_ram_snapshot_locked(self, addr: int, size: int) -> bytes:
        if self.gdb_sock is not None:
            return self._read_virtual_memory_paused_locked(addr, size)
        if not self._is_guest_ram_va(addr, size):
            raise RuntimeError(f"guest RAM VA 0x{addr & 0xFFFFFFFF:08x} is not readable without GDB")
        return self.read_physical_memory(addr & 0x1FFFFFFF, size)

    def _read_c_string_paused_locked(self, addr: int, max_size: int = 0x100) -> bytes:
        data = self._read_virtual_memory_paused_locked(addr, max(1, int(max_size)))
        return data.split(b"\x00", 1)[0]

    def _guest_c_string_trace_paused_locked(self, addr: int, max_size: int = 0x100) -> dict[str, object]:
        out: dict[str, object] = {"addr": f"0x{addr & 0xFFFFFFFF:08x}"}
        if not self._is_guest_ram_va(addr, 1):
            out["reason"] = "not-guest-ram"
            return out
        try:
            raw = self._read_c_string_paused_locked(addr, max_size)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        out["hex"] = raw[: max(0, int(max_size))].hex()
        for encoding in ("gbk", "ascii"):
            try:
                out[encoding] = raw.decode(encoding)
            except UnicodeDecodeError:
                pass
        return out

    def write_virtual_memory(self, addr: int, data: bytes) -> None:
        """Write guest virtual memory through the QEMU GDB stub."""

        if not data:
            return
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            try:
                self._pause_for_gdb_locked()
                self._write_virtual_memory_paused_locked(addr, data)
                self.gdb_write_count += 1
                self.last_gdb_error = None
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def _write_virtual_memory_paused_locked(self, addr: int, data: bytes) -> None:
        if self.gdb_sock is None:
            raise RuntimeError("QEMU GDB stub is not connected")
        payload = _gdb_packet(self.gdb_sock, f"M{addr & 0xFFFFFFFF:x},{len(data):x}:{data.hex()}")
        if payload != "OK":
            raise IOError(f"GDB write failed: {payload}")

    @staticmethod
    def _gdb_u32_to_hex(value: int) -> str:
        return struct.pack("<I", value & 0xFFFFFFFF).hex()

    @staticmethod
    def _gdb_hex_to_u32(payload: str) -> int:
        data = bytes.fromhex(payload)
        if len(data) < 4:
            raise IOError(f"GDB register payload is too short: {payload!r}")
        return struct.unpack("<I", data[:4])[0]

    def read_register(self, regno: int) -> int:
        """Read a 32-bit guest register through the QEMU GDB stub."""

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            try:
                self._pause_for_gdb_locked()
                payload = _gdb_packet(self.gdb_sock, f"p{int(regno):x}")
                if payload.startswith("E"):
                    raise IOError(f"GDB register read failed: {payload}")
                value = self._gdb_hex_to_u32(payload)
                self.gdb_register_read_count += 1
                self.last_gdb_error = None
                return value
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def write_register(self, regno: int, value: int) -> None:
        """Write a 32-bit guest register through the QEMU GDB stub."""

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            try:
                self._pause_for_gdb_locked()
                payload = _gdb_packet(self.gdb_sock, f"P{int(regno):x}={self._gdb_u32_to_hex(value)}")
                if payload != "OK":
                    raise IOError(f"GDB register write failed: {payload}")
                self.gdb_register_write_count += 1
                self.last_gdb_error = None
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def _read_register_paused_locked(self, regno: int) -> int:
        if self.gdb_sock is None:
            raise RuntimeError("QEMU GDB stub is not connected")
        payload = _gdb_packet(self.gdb_sock, f"p{int(regno):x}")
        if payload.startswith("E"):
            raise IOError(f"GDB register read failed: {payload}")
        return self._gdb_hex_to_u32(payload)

    def _write_register_paused_locked(self, regno: int, value: int) -> None:
        if self.gdb_sock is None:
            raise RuntimeError("QEMU GDB stub is not connected")
        payload = _gdb_packet(self.gdb_sock, f"P{int(regno):x}={self._gdb_u32_to_hex(value)}")
        if payload != "OK":
            raise IOError(f"GDB register write failed: {payload}")

    def _step_paused_locked(self) -> str:
        if self.gdb_sock is None:
            raise RuntimeError("QEMU GDB stub is not connected")
        stop_reply = _gdb_step(self.gdb_sock)
        self.gdb_step_count += 1
        return stop_reply

    def write_registers_checked(self, registers: dict[int, int]) -> dict[str, str]:
        """Write registers while stopped and verify them before resuming."""

        checked: dict[str, str] = {}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            try:
                self._pause_for_gdb_locked()
                for regno, value in registers.items():
                    self._write_register_paused_locked(regno, value)
                    self.gdb_register_write_count += 1
                for regno, expected in registers.items():
                    actual = self._read_register_paused_locked(regno)
                    self.gdb_register_read_count += 1
                    if actual != (expected & 0xFFFFFFFF):
                        raise IOError(f"GDB register {regno} readback 0x{actual:08x} != 0x{expected & 0xFFFFFFFF:08x}")
                    checked[str(regno)] = f"0x{actual:08x}"
                self.last_gdb_error = None
                return checked
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def read_pc(self) -> int:
        return self.read_register(37)

    def _read_pc_paused_locked(self) -> int | None:
        try:
            value = self._read_register_paused_locked(37)
            self.gdb_register_read_count += 1
            return value
        except Exception:
            return None

    def write_pc(self, value: int) -> None:
        self.write_register(37, value)

    def call_guest_function_stepped(
        self,
        address: int,
        *,
        args: tuple[int, int, int, int] = (),
        return_pc: int = DEFAULT_C200_BASE,
        max_steps: int = 16,
        max_recorded_steps: int = 64,
    ) -> dict[str, object]:
        """Call a small guest function by GDB single-stepping and restoring state."""

        if len(args) > 4:
            raise ValueError("at most four MIPS o32 argument registers are supported")
        row: dict[str, object] = {
            "address": f"0x{address & 0xFFFFFFFF:08x}",
            "return_pc": f"0x{return_pc & 0xFFFFFFFF:08x}",
            "max_steps": max_steps,
            "returned": False,
        }
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                row["error"] = "QEMU process is not running"
                return row
            if self.gdb_sock is None:
                row["error"] = "QEMU GDB stub is not connected"
                return row
            try:
                self._pause_for_gdb_locked()
                row.update(
                    self._call_guest_function_stepped_paused_locked(
                        address,
                        args=args,
                        return_pc=return_pc,
                        max_steps=max_steps,
                        max_recorded_steps=max_recorded_steps,
                    )
                )
                return row
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                row["error"] = self.last_gdb_error
                return row
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def _call_guest_function_stepped_paused_locked(
        self,
        address: int,
        *,
        args: tuple[int, int, int, int] = (),
        return_pc: int = DEFAULT_C200_BASE,
        max_steps: int = 16,
        max_recorded_steps: int = 64,
        continue_timeout: float | None = None,
    ) -> dict[str, object]:
        if len(args) > 4:
            raise ValueError("at most four MIPS o32 argument registers are supported")
        saved_regs = (2, 4, 5, 6, 7, 29, 31, 37)
        saved: dict[int, int] = {}
        row: dict[str, object] = {"returned": False}
        try:
            for regno in saved_regs:
                saved[regno] = self._read_register_paused_locked(regno)
                self.gdb_register_read_count += 1
            for idx, value in enumerate(args):
                self._write_register_paused_locked(4 + idx, value)
                self.gdb_register_write_count += 1
            self._write_register_paused_locked(31, return_pc)
            self._write_register_paused_locked(37, address)
            self.gdb_register_write_count += 2
            if continue_timeout is not None:
                stop_reply = ""
                breakpoint_inserted = False
                try:
                    stop_reply = _gdb_insert_breakpoint(self.gdb_sock, return_pc & 0xFFFFFFFF)
                    breakpoint_inserted = True
                    stop_reply = _gdb_continue_wait(self.gdb_sock, timeout=max(0.1, float(continue_timeout)))
                    current_pc = self._read_register_paused_locked(37)
                    self.gdb_register_read_count += 1
                    v0 = self._read_register_paused_locked(2)
                    self.gdb_register_read_count += 1
                    row.update(
                        {
                            "mode": "continue",
                            "stop": stop_reply,
                            "step_count": None,
                            "steps_omitted": 0,
                            "steps": [],
                            "final_pc": f"0x{current_pc:08x}",
                            "v0": f"0x{v0:08x}",
                            "returned": current_pc == (return_pc & 0xFFFFFFFF),
                        }
                    )
                    if not row["returned"]:
                        row["error"] = "guest function stopped before return_pc"
                    self.guest_call_count += 1
                    self.last_gdb_error = None
                    return row
                except socket.timeout:
                    try:
                        _gdb_interrupt(self.gdb_sock)
                    except Exception:
                        pass
                    row.update({"mode": "continue", "stop": stop_reply, "error": "guest function did not hit return_pc before timeout"})
                    self.guest_call_count += 1
                    return row
                finally:
                    if breakpoint_inserted:
                        try:
                            _gdb_remove_breakpoint(self.gdb_sock, return_pc & 0xFFFFFFFF)
                        except Exception:
                            pass
            steps: list[dict[str, object]] = []
            max_steps = max(1, int(max_steps))
            max_recorded_steps = max(0, int(max_recorded_steps))
            head_limit = min(max_recorded_steps, max_recorded_steps // 2)
            tail_limit = max(0, max_recorded_steps - head_limit)
            tail: list[dict[str, object]] = []
            omitted = 0
            current_pc = address & 0xFFFFFFFF
            for step_index in range(max_steps):
                stop_reply = self._step_paused_locked()
                current_pc = self._read_register_paused_locked(37)
                self.gdb_register_read_count += 1
                step = {
                    "index": step_index + 1,
                    "pc": f"0x{current_pc:08x}",
                    "stop": stop_reply,
                }
                if max_recorded_steps:
                    if len(steps) < head_limit:
                        steps.append(step)
                    elif tail_limit:
                        tail.append(step)
                        if len(tail) > tail_limit:
                            tail.pop(0)
                            omitted += 1
                    else:
                        omitted += 1
                if current_pc == (return_pc & 0xFFFFFFFF):
                    row["returned"] = True
                    break
            v0 = self._read_register_paused_locked(2)
            self.gdb_register_read_count += 1
            if tail:
                steps.extend(tail)
            row.update(
                {
                    "step_count": step_index + 1,
                    "steps_omitted": omitted,
                    "steps": steps,
                    "final_pc": f"0x{current_pc:08x}",
                    "v0": f"0x{v0:08x}",
                }
            )
            if not row["returned"]:
                row["error"] = "guest function did not reach return_pc before max_steps"
            self.guest_call_count += 1
            self.last_gdb_error = None
            return row
        finally:
            for regno, value in saved.items():
                self._write_register_paused_locked(regno, value)
            if saved:
                self.gdb_register_write_count += len(saved)

    def _pause_for_gdb_locked(self) -> None:
        if self.gdb_sock is not None:
            _gdb_interrupt(self.gdb_sock)
            return
        if self.hmp_sock is None:
            raise RuntimeError("QEMU debug monitor is not connected")
        _hmp_command(self.hmp_sock, "stop")
        time.sleep(0.02)

    def _resume_after_gdb_locked(self) -> None:
        if self.gdb_sock is not None:
            _gdb_continue(self.gdb_sock)
            return
        if self.hmp_sock is None:
            return
        _hmp_command(self.hmp_sock, "cont")

    def _is_guest_ram_va(self, va: int, size: int = 1) -> bool:
        if va == 0 or va < 0x80000000:
            return False
        phys = va & 0x1FFFFFFF
        return 0 <= phys and phys + size <= self.config.ram_mb * 1024 * 1024

    def _fat16_image_path(self) -> Path | None:
        candidates = [
            *DEFAULT_QEMU_NAND_IMAGE_CANDIDATES,
            Path("build") / "bbk9588_nand_c200_fat_page1c40_root512_ftloob.bin",
            Path("build") / "bbk9588_nand_c200_fat_page1c40_root256_ftloob.bin",
            Path("build") / "bbk9588_nand_c200_fat_page1c40.bin",
            Path("build") / "bbk9588_fs_fat16.img",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _nand_fat_sector0_index(self, image: Path) -> int | None:
        if self.diagnostic_nand_fat_sector0_cache is not None:
            return self.diagnostic_nand_fat_sector0_cache
        stride = 2048 + 64
        sectors_per_page = 4
        try:
            size = image.stat().st_size
            with image.open("rb") as fh:
                for page in range(size // stride):
                    page_off = page * stride
                    fh.seek(page_off)
                    body = fh.read(2048)
                    if len(body) < 2048:
                        break
                    for sector_in_page in range(sectors_per_page):
                        off = sector_in_page * 512
                        sector = body[off : off + 512]
                        if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
                            continue
                        if sector[54:59] != b"FAT16" and sector[82:87] != b"FAT16":
                            continue
                        hidden = struct.unpack_from("<I", sector, 28)[0]
                        absolute_sector = page * sectors_per_page + sector_in_page
                        sector0 = absolute_sector - hidden
                        if sector0 >= 0:
                            self.diagnostic_nand_fat_sector0_cache = sector0
                            return sector0
        except OSError:
            return None
        return None

    def _read_backing_sector(self, sector: int) -> bytes | None:
        if sector < 0:
            return None
        cached = self.diagnostic_backing_sector_cache.get(sector)
        if cached is not None:
            return cached
        image = self._fat16_image_path()
        if image is None:
            return None
        if "nand" in image.name.lower():
            sector0 = self._nand_fat_sector0_index(image)
            if sector0 is None:
                return None
            relative = sector0 + int(sector)
            if relative < 0:
                return None
            page = relative // 4
            sector_in_page = relative % 4
            offset = page * (2048 + 64) + sector_in_page * 512
        else:
            offset = int(sector) * 512
        try:
            with image.open("rb") as fh:
                fh.seek(offset)
                data = fh.read(512)
        except OSError:
            return None
        if len(data) != 512:
            return None
        if len(self.diagnostic_backing_sector_cache) > 2048:
            self.diagnostic_backing_sector_cache.clear()
        self.diagnostic_backing_sector_cache[sector] = data
        return data

    def _backing_sector_capacity(self) -> int | None:
        image = self._fat16_image_path()
        if image is None:
            return None
        try:
            size = image.stat().st_size
        except OSError:
            return None
        if "nand" not in image.name.lower():
            return size // 512
        sector0 = self._nand_fat_sector0_index(image)
        if sector0 is None:
            return None
        total = (size // (2048 + 64)) * 4
        if total <= sector0:
            return None
        return total - sector0

    def _fat16_layout_from_backing(self) -> dict[str, int] | None:
        if self.diagnostic_fat16_layout_cache is not None:
            return self.diagnostic_fat16_layout_cache
        candidates = [0x20, 0]
        candidates.extend(lba for lba in range(1, 0x100) if lba not in candidates)
        for volume_lba in candidates:
            boot = self._read_backing_sector(volume_lba)
            if boot is None or len(boot) < 512 or boot[510:512] != b"\x55\xaa":
                continue
            bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0]
            sectors_per_cluster = boot[0x0D]
            reserved_sectors = struct.unpack_from("<H", boot, 0x0E)[0]
            fat_count = boot[0x10]
            root_entries = struct.unpack_from("<H", boot, 0x11)[0]
            total_sectors_16 = struct.unpack_from("<H", boot, 0x13)[0]
            sectors_per_fat_16 = struct.unpack_from("<H", boot, 0x16)[0]
            total_sectors_32 = struct.unpack_from("<I", boot, 0x20)[0]
            if bytes_per_sector != 512 or sectors_per_cluster == 0 or fat_count == 0 or sectors_per_fat_16 == 0:
                continue
            total_sectors = total_sectors_16 or total_sectors_32
            root_dir_sectors = ((root_entries * 32) + (bytes_per_sector - 1)) // bytes_per_sector
            fat_lba = volume_lba + reserved_sectors
            root_lba = fat_lba + fat_count * sectors_per_fat_16
            first_data_lba = root_lba + root_dir_sectors
            if first_data_lba >= total_sectors + volume_lba:
                continue
            self.diagnostic_fat16_layout_cache = {
                "volume_lba": volume_lba,
                "bytes_per_sector": bytes_per_sector,
                "sectors_per_cluster": sectors_per_cluster,
                "fat_lba": fat_lba,
                "root_lba": root_lba,
                "root_dir_sectors": root_dir_sectors,
                "first_data_lba": first_data_lba,
                "total_sectors": total_sectors,
            }
            return self.diagnostic_fat16_layout_cache
        return None

    @staticmethod
    def _firmware_dirent_from_fat_dirent(data: bytes) -> bytes:
        if len(data) < 0x20:
            raise ValueError("FAT directory entry must be at least 32 bytes")
        out = bytearray(0x20)
        out[0:8] = data[0:8]
        out[8:11] = data[8:11]
        out[0x0B] = data[0x0B]
        out[0x0C] = data[0x0C]
        out[0x0D] = data[0x0D]
        out[0x0E:0x10] = data[0x0E:0x10]
        out[0x10:0x12] = data[0x10:0x12]
        out[0x12:0x14] = data[0x12:0x14]
        high_cluster = struct.unpack_from("<H", data, 0x14)[0]
        low_cluster = struct.unpack_from("<H", data, 0x1A)[0]
        struct.pack_into("<I", out, 0x14, ((high_cluster << 16) | low_cluster) & 0xFFFFFFFF)
        out[0x18:0x1A] = data[0x16:0x18]
        out[0x1A:0x1C] = data[0x18:0x1A]
        out[0x1C:0x20] = data[0x1C:0x20]
        if out[0] == 5:
            out[0] = 0xE5
        return bytes(out)

    @staticmethod
    def _short_path_component_from_firmware_dirent(data: bytes) -> bytes | None:
        if len(data) < 0x20:
            return None
        name = data[:8].rstrip(b" ")
        ext = data[8:11].rstrip(b" ")
        if not name:
            return None
        if ext:
            return name + b"." + ext
        return name

    @staticmethod
    def _decode_lfn_entries(entries: list[bytes]) -> str | None:
        if not entries:
            return None
        units: list[int] = []
        for entry in reversed(entries):
            if len(entry) < 0x20:
                continue
            for pos in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30):
                value = struct.unpack_from("<H", entry, pos)[0]
                if value in (0, 0xFFFF):
                    continue
                units.append(value)
        if not units:
            return None
        raw = bytes(byte for unit in units for byte in (unit & 0xFF, unit >> 8))
        return raw.decode("utf-16le", errors="replace")

    def _fat16_long_name_aliases_by_raw(self) -> dict[bytes, list[bytes]]:
        cached = self.diagnostic_fat16_long_name_alias_cache
        if cached is not None:
            return cached
        aliases: dict[bytes, list[bytes]] = {}
        visited: set[int] = set()

        def add_alias(raw: bytes, text: str | None) -> None:
            if not text:
                return
            encoded_values = [
                text.encode("gbk", errors="replace"),
                text.encode("ascii", errors="ignore"),
            ]
            bucket = aliases.setdefault(raw.upper(), [])
            for value in encoded_values:
                if value and value not in bucket:
                    bucket.append(value)

        def scan_dir(data: bytes, depth: int) -> None:
            if depth > 8:
                return
            lfns: list[bytes] = []
            for offset in range(0, len(data), 0x20):
                entry = data[offset : offset + 0x20]
                if len(entry) < 0x20 or entry[0] == 0x00:
                    break
                if entry[0] == 0xE5:
                    lfns.clear()
                    continue
                attr = entry[0x0B]
                if attr == 0x0F:
                    lfns.append(entry)
                    continue
                raw = bytes(entry[:11])
                long_name = self._decode_lfn_entries(lfns)
                lfns.clear()
                add_alias(raw, long_name)
                if not self._is_usable_fat_dirent(entry) or not (attr & 0x10):
                    continue
                cluster = struct.unpack_from("<H", entry, 0x1A)[0]
                if cluster < 2 or cluster in visited:
                    continue
                visited.add(cluster)
                child = self._fat16_cluster_data_from_backing(cluster)
                if child is not None:
                    scan_dir(child, depth + 1)

        root_data = self._root_directory_data_from_backing()
        if root_data is not None:
            scan_dir(root_data, 0)
        self.diagnostic_fat16_long_name_alias_cache = aliases
        return aliases

    @staticmethod
    def _is_usable_fat_dirent(entry: bytes) -> bool:
        if len(entry) < 0x20:
            return False
        first = entry[0]
        attr = entry[0x0B]
        if first in (0x00, 0xE5):
            return False
        if attr == 0x0F or (attr & 0x08):
            return False
        name = entry[:8].rstrip(b" ")
        if name in (b".", b".."):
            return False
        return True

    def _fat16_cluster_data_from_backing(self, cluster: int) -> bytes | None:
        layout = self._fat16_layout_from_backing()
        if layout is None or cluster < 2 or cluster >= 0xFFF8:
            return None
        lba = layout["first_data_lba"] + (cluster - 2) * layout["sectors_per_cluster"]
        chunks: list[bytes] = []
        for sector_index in range(layout["sectors_per_cluster"]):
            data = self._read_backing_sector(lba + sector_index)
            if data is None:
                return None
            chunks.append(data)
        return b"".join(chunks)

    def _fat16_next_cluster_from_backing(self, cluster: int) -> int | None:
        layout = self._fat16_layout_from_backing()
        if layout is None or cluster < 2:
            return None
        fat_offset = cluster * 2
        sector = layout["fat_lba"] + (fat_offset // layout["bytes_per_sector"])
        offset = fat_offset % layout["bytes_per_sector"]
        data = self._read_backing_sector(sector)
        if data is None or offset + 2 > len(data):
            return None
        return struct.unpack_from("<H", data, offset)[0]

    def _read_backing_file_bytes(self, entry: dict[str, object]) -> bytes | None:
        cluster = int(entry.get("cluster", 0))
        size = int(entry.get("size", 0))
        if cluster < 2 or size < 0:
            return None
        chunks: list[bytes] = []
        visited: set[int] = set()
        remaining = size
        while cluster >= 2 and cluster < 0xFFF8 and cluster not in visited and remaining > 0:
            visited.add(cluster)
            data = self._fat16_cluster_data_from_backing(cluster)
            if data is None:
                return None
            chunks.append(data[:remaining])
            remaining -= min(remaining, len(data))
            if remaining <= 0:
                break
            next_cluster = self._fat16_next_cluster_from_backing(cluster)
            if next_cluster is None:
                return None
            cluster = next_cluster
        if remaining > 0:
            return None
        return b"".join(chunks)[:size]

    def _first_dirent_from_directory_data(self, data: bytes, *, require_directory: bool = False) -> dict[str, object] | None:
        for offset in range(0, len(data), 0x20):
            entry = data[offset : offset + 0x20]
            if len(entry) < 0x20:
                continue
            if entry[0] == 0x00:
                return None
            if not self._is_usable_fat_dirent(entry):
                continue
            firmware = self._firmware_dirent_from_fat_dirent(entry)
            attr = firmware[0x0B]
            if require_directory and not (attr & 0x10):
                continue
            return {
                "offset": offset,
                "raw": entry,
                "firmware": firmware,
                "name_hex": entry[:11].hex(),
                "attr": attr,
                "cluster": struct.unpack_from("<I", firmware, 0x14)[0],
                "size": struct.unpack_from("<I", firmware, 0x1C)[0],
            }
        return None

    def _first_root_dirent_from_backing(self) -> dict[str, object] | None:
        layout = self._fat16_layout_from_backing()
        if layout is None:
            return None
        for sector_index in range(layout["root_dir_sectors"]):
            sector = layout["root_lba"] + sector_index
            data = self._read_backing_sector(sector)
            if data is None:
                continue
            for offset in range(0, len(data), 0x20):
                entry = data[offset : offset + 0x20]
                if len(entry) < 0x20:
                    continue
                first = entry[0]
                if first == 0x00:
                    return None
                if not self._is_usable_fat_dirent(entry):
                    continue
                firmware = self._firmware_dirent_from_fat_dirent(entry)
                return {
                    "sector": sector,
                    "offset": offset,
                    "raw": entry,
                    "firmware": firmware,
                    "name_hex": entry[:11].hex(),
                    "attr": firmware[0x0B],
                    "cluster": struct.unpack_from("<I", firmware, 0x14)[0],
                    "size": struct.unpack_from("<I", firmware, 0x1C)[0],
                }
        return None

    def _first_child_dirent_from_backing(
        self, parent: dict[str, object], *, require_directory: bool = False
    ) -> dict[str, object] | None:
        cluster = parent.get("cluster")
        if not isinstance(cluster, int):
            return None
        data = self._fat16_cluster_data_from_backing(cluster)
        if data is None:
            return None
        child = self._first_dirent_from_directory_data(data, require_directory=require_directory)
        if child is None:
            return None
        child["parent_cluster"] = cluster
        return child

    def _root_directory_data_from_backing(self) -> bytes | None:
        layout = self._fat16_layout_from_backing()
        if layout is None:
            return None
        chunks: list[bytes] = []
        for sector_index in range(layout["root_dir_sectors"]):
            data = self._read_backing_sector(layout["root_lba"] + sector_index)
            if data is None:
                return None
            chunks.append(data)
        return b"".join(chunks)

    def _directory_entries_from_data(self, data: bytes) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for offset in range(0, len(data), 0x20):
            entry = data[offset : offset + 0x20]
            if len(entry) < 0x20:
                continue
            if entry[0] == 0x00:
                break
            if not self._is_usable_fat_dirent(entry):
                continue
            firmware = self._firmware_dirent_from_fat_dirent(entry)
            name = self._short_path_component_from_firmware_dirent(firmware)
            if name is None:
                continue
            entries.append(
                {
                    "offset": offset,
                    "raw": entry,
                    "firmware": firmware,
                    "name": name,
                    "name_hex": entry[:11].hex(),
                    "attr": firmware[0x0B],
                    "cluster": struct.unpack_from("<I", firmware, 0x14)[0],
                    "size": struct.unpack_from("<I", firmware, 0x1C)[0],
                }
            )
        return entries

    def _first_file_path_from_backing(self, *, max_depth: int = 4) -> dict[str, object] | None:
        root_data = self._root_directory_data_from_backing()
        if root_data is None:
            return None
        visited: set[int] = set()

        def walk(data: bytes, parts: list[bytes], depth: int) -> dict[str, object] | None:
            if depth > max_depth:
                return None
            for entry in self._directory_entries_from_data(data):
                name = entry.get("name")
                cluster = entry.get("cluster")
                attr = entry.get("attr")
                if not isinstance(name, bytes) or not isinstance(cluster, int) or not isinstance(attr, int):
                    continue
                next_parts = [*parts, name]
                if attr & 0x10:
                    if cluster < 2 or cluster in visited:
                        continue
                    visited.add(cluster)
                    child_data = self._fat16_cluster_data_from_backing(cluster)
                    if child_data is None:
                        continue
                    found = walk(child_data, next_parts, depth + 1)
                    if found is not None:
                        return found
                    continue
                path = b"\\" + b"\\".join(next_parts) + b"\x00"
                return {
                    "path": path,
                    "parts_hex": [part.hex() for part in next_parts],
                    "cluster": cluster,
                    "size": entry.get("size", 0),
                    "name_hex": entry.get("name_hex", ""),
                }
            return None

        return walk(root_data, [], 0)

    def _find_path_from_backing(self, path: bytes | str, *, max_depth: int = 8) -> dict[str, object] | None:
        if isinstance(path, str):
            path_bytes = path.encode("gbk", errors="replace")
        else:
            path_bytes = bytes(path)
        path_bytes = path_bytes.split(b"\x00", 1)[0]
        parts = [part for part in path_bytes.replace(b"/", b"\\").split(b"\\") if part]
        if parts and len(parts[0]) == 2 and parts[0][1:2] == b":":
            parts = parts[1:]
        if not parts:
            return None
        data = self._root_directory_data_from_backing()
        if data is None:
            return None
        aliases = self._fat16_long_name_aliases_by_raw()
        traversed: list[bytes] = []
        visited: set[int] = set()

        def entry_names(entry: dict[str, object]) -> list[bytes]:
            raw_obj = entry.get("raw")
            if not isinstance(raw_obj, (bytes, bytearray)):
                return []
            raw = bytes(raw_obj[:11])
            name = raw[:8].rstrip(b" ")
            ext = raw[8:11].rstrip(b" ")
            short_name = name + (b"." + ext if ext else b"")
            names = [short_name, name]
            names.extend(aliases.get(raw.upper(), []))
            return [value.upper() for value in names if value]

        for depth, part in enumerate(parts):
            if depth > max_depth:
                return None
            target = part.upper()
            matched: dict[str, object] | None = None
            entries = self._directory_entries_from_data(data)
            for entry in entries:
                if target in entry_names(entry):
                    matched = entry
                    break
            if matched is None:
                return None
            name = matched.get("name")
            if isinstance(name, bytes):
                traversed.append(name)
            else:
                traversed.append(part)
            is_last = depth == len(parts) - 1
            attr = int(matched.get("attr", 0))
            cluster = int(matched.get("cluster", 0))
            if is_last:
                result_path = b"\\" + b"\\".join(parts) + b"\x00"
                return {
                    "path": result_path,
                    "parts_hex": [value.hex() for value in parts],
                    "short_parts_hex": [value.hex() for value in traversed],
                    "cluster": cluster,
                    "size": int(matched.get("size", 0)),
                    "name_hex": str(matched.get("name_hex", "")),
                    "attr": attr,
                    "firmware": matched.get("firmware"),
                    "raw": matched.get("raw"),
                }
            if not (attr & 0x10) or cluster < 2 or cluster in visited:
                return None
            visited.add(cluster)
            next_data = self._fat16_cluster_data_from_backing(cluster)
            if next_data is None:
                return None
            data = next_data
        return None

    def _system_boot_file_paths_from_backing(self) -> list[bytes]:
        candidates = [
            "\\系统\\数据\\SysTp.cfg",
            "\\系统\\数据\\Config.inf",
            "\\系统\\数据\\Resource.dat",
            "\\系统\\数据\\Resource1.dat",
            "\\系统\\数据\\Resource2.dat",
            "\\系统\\Desktop\\c200dts1a.dlx",
            "\\系统\\Desktop\\dskin.dlx",
            "\\系统\\Desktop\\icons.dlx",
            "\\系统\\Desktop\\shella.dlx",
        ]
        out: list[bytes] = []
        for candidate in candidates:
            found = self._find_path_from_backing(candidate)
            path = found.get("path") if isinstance(found, dict) else None
            if isinstance(path, (bytes, bytearray)):
                out.append(bytes(path))
        return out

    def _system_boot_file_entries_from_backing(self) -> list[dict[str, object]]:
        candidates = [
            "\\系统\\数据\\SysTp.cfg",
            "\\系统\\数据\\Config.inf",
            "\\系统\\数据\\Resource.dat",
            "\\系统\\数据\\Resource1.dat",
            "\\系统\\数据\\Resource2.dat",
            "\\系统\\Desktop\\c200dts1a.dlx",
            "\\系统\\Desktop\\dskin.dlx",
            "\\系统\\Desktop\\icons.dlx",
            "\\系统\\Desktop\\shella.dlx",
        ]
        out: list[dict[str, object]] = []
        for candidate in candidates:
            found = self._find_path_from_backing(candidate)
            if isinstance(found, dict):
                out.append(found)
        return out

    def _seed_legacy_python_storage_hook_globals_paused_locked(self) -> dict[str, object]:
        return {
            "event": "qemu-legacy-python-storage-hook-seed",
            "seeded": False,
            "disabled": True,
            "reason": "Legacy Python/GDB storage hooks were removed from the hardware-model path",
        }

    def _handle_legacy_python_storage_hook_break_paused_locked(self, pc: int) -> dict[str, object]:
        return {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "handled": False,
            "disabled": True,
            "reason": "Legacy Python/GDB storage hooks were removed from the hardware-model path",
        }

    def _legacy_python_storage_hook_pcs_for_machine(self) -> tuple[int, ...]:
        pcs = (
            0x8017CA10,
            0x8017B4E0,
            0x80172840,
            0x8017BEF4,
            0x80175E40,
            0x80175D9C,
            0x801747C4,
            0x80174C9C,
            0x80174CC0,
            0x80174CE4,
            0x80182A90,
            0x80182D58,
            0x8012CCFC,
            0x8000BA84,
            0x8000BB98,
            0x80007648,
            0x800067F4,
            0x8000F7F8,
            0x8000F8A0,
            0x8000F0B0,
            0x80182D6C,
        )
        if self.config.machine.lower() == "bbk9588":
            return ()
        return pcs

    def _c_machine_omitted_storage_breakpoints(self) -> list[str]:
        if self.config.machine.lower() != "bbk9588":
            return []
        return [
            "0x8017ca10",
            "0x8017b4e0",
            "0x80172840",
            "0x8017bef4",
            "0x80175e40",
            "0x80175d9c",
            "0x801747c4",
            "0x80174c9c",
            "0x80174cc0",
            "0x80174ce4",
            "0x80182a90",
            "0x80182d58",
            "0x8012ccfc",
            "0x8000ba84",
            "0x8000bb98",
            "0x80007648",
            "0x800067f4",
            "0x8000f7f8",
            "0x8000f8a0",
            "0x8000f0b0",
            "0x80182d6c",
            "0x8000818c",
            "resource-trace-pcs",
        ]

    def _scheduler_dispatch_pcs_for_machine(self) -> tuple[int, ...]:
        if self.config.machine.lower() == "bbk9588":
            return ()
        return (0x8000818C,)

    def _service_legacy_python_storage_hooks_paused_locked(self, *, timeout: float = 0.8, max_hits: int = 64) -> dict[str, object]:
        return {
            "event": "qemu-legacy-python-storage-hook-service",
            "disabled": True,
            "reason": "Legacy Python/GDB storage hooks were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }

    @staticmethod
    def _resource_trace_pcs() -> tuple[int, ...]:
        return (
            0x8012CEB4,
            0x8017B198,
            0x801708BC,
            0x80171018,
            0x80171620,
            0x801716FC,
            0x801717F4,
            0x80171800,
            0x801718B4,
            0x801718BC,
            0x801718A0,
            0x801738BC,
            0x80173920,
            0x80173928,
            0x80173950,
            0x8017395C,
            0x80173A90,
            0x80173BE8,
            0x80173C50,
            0x80173E84,
            0x80173E90,
            0x80175480,
            0x80175494,
            0x801754DC,
            0x80178FC4,
            0x80179000,
            0x80179044,
            0x80179048,
            0x80179280,
            0x801792B4,
            0x8017954C,
            0x801795D4,
            0x801795E8,
            0x80179618,
            0x8017967C,
            0x801796B8,
            0x80179718,
            0x8017975C,
            0x801705EC,
            0x8017062C,
            0x80178F50,
            0x8017FBC0,
            0x8018024C,
            0x801806EC,
            0x8016FC94,
            0x8000FC74,
            0x8000FCA4,
            0x8000FCC4,
            0x80010018,
            0x800100DC,
            0x8001028C,
            0x8001029C,
            0x800102BC,
            0x800102E8,
            0x8001032C,
            0x800103B4,
            0x800103CC,
            0x800103F0,
            0x80010414,
            0x8001041C,
            0x80010430,
            0x80010444,
            0x8000818C,
            0x8001E8B4,
            0x8001E8C0,
            0x8001E8D0,
            0x8001E8EC,
            0x800E1A94,
            0x800E1BF0,
            0x800E1C68,
            0x800E1C84,
            0x800E3C68,
            0x800E447C,
            0x800E5C44,
            0x800E5C58,
            0x800DFC68,
            0x8001E90C,
            0x8001E920,
            0x8001E928,
            0x8001E930,
            0x8001E900,
            0x800228B4,
            0x800228D8,
            0x800228E0,
            0x80172630,
            0x80172670,
            0x8017268C,
            0x801726F4,
            0x80172700,
            0x8017E000,
            0x8017B454,
            0x801813E0,
            0x80181400,
        )

    def _resource_trace_pcs_for_machine(self) -> tuple[int, ...]:
        pcs = self._resource_trace_pcs()
        if self.config.machine.lower() == "bbk9588":
            return ()
        return pcs

    def _resource_trace_service_pcs_for_machine(self) -> tuple[int, ...]:
        if self.config.machine.lower() == "bbk9588":
            return ()
        prefix = (0x8000BA84, 0x8012CCFC)
        return tuple(dict.fromkeys((*prefix, *self._resource_trace_pcs_for_machine())))

    @staticmethod
    def _fs_trace_pcs() -> tuple[int, ...]:
        return (
            0x80173504,
            0x80173630,
            0x80173638,
            0x80173640,
            0x80173710,
            0x80173764,
            0x80173768,
            0x80173F14,
            0x80173F1C,
            0x80173F24,
            0x80173F2C,
        )

    @staticmethod
    def _fs_branch_pcs() -> frozenset[int]:
        return frozenset(
            {
                0x80173630,
                0x80173640,
                0x80173710,
                0x80173768,
                0x80173F14,
                0x80173F1C,
                0x80173F24,
                0x80173F2C,
            }
        )

    def _bbk9588_python_guest_service_disabled(self, event: str) -> dict[str, object]:
        return {
            "event": event,
            "disabled": True,
            "skipped": True,
            "source": "qemu-c-machine",
            "reason": "bbk9588-c-machine-default-path",
            "handled": False,
        }

    def service_legacy_python_storage_hooks(self, *, timeout: float = 0.8, max_hits: int = 64) -> dict[str, object]:
        return {
            "event": "qemu-legacy-python-storage-hook-service",
            "disabled": True,
            "reason": "Legacy Python/GDB storage hooks were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }

    def _service_legacy_python_resource_hook_paused_locked(
        self,
        *,
        timeout: float = 1.0,
        max_hits: int = 256,
        entry: int = 0x80179618,
        args: tuple[int, int, int, int] = (),
        event: str = "qemu-legacy-python-resource-hook-service",
    ) -> dict[str, object]:
        return {
            "event": event,
            "entry": f"0x{entry & 0xFFFFFFFF:08x}",
            "disabled": True,
            "reason": "Legacy Python/GDB resource hook services were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }

    def _service_legacy_python_resource_hook_rounds_paused_locked(
        self, *, rounds: int = 3, timeout_per_round: float = 1.0, max_hits_per_round: int = 256
    ) -> dict[str, object]:
        out: dict[str, object] = {
            "event": "qemu-legacy-python-resource-hook-rounds-service",
            "rounds": [],
        }
        c_ready = self._qemu_c_resource_refresh_ready_paused_locked()
        if self.config.machine.lower() == "bbk9588":
            reason = "qemu-c-resource-refresh-ready" if c_ready.get("ready") else "bbk9588-c-machine-default-path"
            out.update(
                {
                    "skipped": True,
                    "disabled": True,
                    "reason": reason,
                    "source": "qemu-c-machine",
                    "resource_refresh": c_ready,
                    "handled_count": 0,
                }
            )
            try:
                out["final_pc"] = self._format_u32(self._read_pc_paused_locked())
            except Exception:
                out["final_pc"] = None
            return out
        if c_ready.get("ready"):
            out.update(
                {
                    "skipped": True,
                    "reason": "qemu-c-resource-refresh-ready",
                    "source": "qemu-c-machine",
                    "resource_refresh": c_ready,
                    "handled_count": 0,
                    "final_pc": self._format_u32(self._read_pc_paused_locked()),
                }
            )
            return out
        for index in range(max(0, int(rounds))):
            prime = self._prime_resource_refresh_paused_locked()
            row = self._service_legacy_python_resource_hook_paused_locked(timeout=timeout_per_round, max_hits=max_hits_per_round)
            row["round"] = index
            if prime:
                row["prime"] = prime
            out["rounds"].append(row)
            if row.get("error"):
                out["error"] = row.get("error")
                break
            if not row.get("events"):
                break
        out["handled_count"] = sum(
            int(round_.get("handled_count", 0))
            for round_ in out["rounds"]
            if isinstance(round_, dict)
        )
        out["final_pc"] = self._format_u32(self._read_pc_paused_locked())
        return out

    def _qemu_c_resource_refresh_ready_paused_locked(self) -> dict[str, object]:
        row: dict[str, object] = {"ready": False}
        if self.config.machine.lower() != "bbk9588":
            row["reason"] = "not-bbk9588-machine"
            return row
        flags = self._read_u32_paused_locked(0x804BF440) or 0
        refresh = self._read_u8_paused_locked(0x804BF444) or 0
        row.update(
            {
                "flags_804bf440": f"0x{flags & 0xFFFFFFFF:08x}",
                "refresh_804bf444": f"0x{refresh & 0xFF:02x}",
            }
        )
        if flags == 0 and refresh == 1:
            row["ready"] = True
            row["source"] = "qemu-c-machine"
        else:
            row["reason"] = "resource-refresh-not-ready"
        return row

    def _service_fs_scan_probe_paused_locked(
        self,
        *,
        timeout: float = 2.0,
        max_hits: int = 512,
        pattern_bytes: bytes | None = None,
        event: str = "qemu-fs-scan-probe",
    ) -> dict[str, object]:
        obj = 0x809533F4
        pattern = 0x8095344C
        result = 0x8078A0A8
        if pattern_bytes is None:
            pattern_bytes = b"\\*.*\x00"
        elif not pattern_bytes.endswith(b"\x00"):
            pattern_bytes += b"\x00"
        row: dict[str, object] = {
            "event": event,
            "entry": "0x80173504",
            "object": f"0x{obj:08x}",
            "pattern": f"0x{pattern:08x}",
            "pattern_hex": pattern_bytes.hex(),
            "result": f"0x{result:08x}",
        }
        if self.config.machine.lower() == "bbk9588":
            row.update(self._bbk9588_python_guest_service_disabled(event))
            return row
        if not (self._is_guest_ram_va(obj, 0x100) and self._is_guest_ram_va(result, 0x40)):
            row["error"] = "probe buffers are outside guest RAM"
            return row
        self._write_virtual_memory_paused_locked(obj, bytes(0x100))
        self._write_virtual_memory_paused_locked(result, bytes(0x40))
        self._write_virtual_memory_paused_locked(pattern, pattern_bytes)
        call = self._service_legacy_python_resource_hook_paused_locked(
            timeout=timeout,
            max_hits=max_hits,
            entry=0x80173504,
            args=(obj, pattern, result, 0),
            event="qemu-fs-scan-probe-call",
        )
        row["call"] = call
        native = self._first_root_dirent_from_backing()
        if native is not None:
            row["native_fs_scan_fallback"] = {
                "available": True,
                "sector": f"0x{int(native['sector']):x}",
                "offset": f"0x{int(native['offset']):x}",
                "name_hex": str(native["name_hex"]),
                "attr": f"0x{int(native['attr']):02x}",
                "cluster": f"0x{int(native['cluster']):08x}",
                "size": f"0x{int(native['size']):08x}",
            }
            returned_v0 = None
            events = call.get("events") if isinstance(call.get("events"), list) else []
            for event_row in reversed(events):
                if isinstance(event_row, dict) and event_row.get("kind") == "resource-pump-return":
                    returned_v0 = event_row.get("v0")
                    break
            should_apply_fallback = returned_v0 in (None, "0xffffffff") and self._is_guest_ram_va(result, 0x20)
            if should_apply_fallback:
                self._write_virtual_memory_paused_locked(result, native["firmware"])  # type: ignore[arg-type]
                row["native_fs_scan_fallback"]["applied"] = True  # type: ignore[index]
            else:
                row["native_fs_scan_fallback"]["applied"] = False  # type: ignore[index]
        else:
            row["native_fs_scan_fallback"] = {"available": False}
        try:
            data = self._read_virtual_memory_paused_locked(result, 0x20)
            row["result_words"] = [f"0x{struct.unpack_from('<I', data, off)[0]:08x}" for off in range(0, 0x20, 4)]
            row["result_dirent"] = {
                "name_hex": data[:11].hex(),
                "attr": f"0x{data[0x0B]:02x}",
                "cluster": f"0x{struct.unpack_from('<I', data, 0x14)[0]:08x}",
                "size": f"0x{struct.unpack_from('<I', data, 0x1C)[0]:08x}",
            }
        except Exception as exc:
            row["result_error"] = f"{type(exc).__name__}: {exc}"
        return row

    def _first_root_directory_scan_pattern_from_backing(self) -> bytes | None:
        native = self._first_root_dirent_from_backing()
        if native is None:
            return None
        firmware = native.get("firmware")
        if not isinstance(firmware, (bytes, bytearray)) or len(firmware) < 0x20:
            return None
        if not (firmware[0x0B] & 0x10):
            return None
        name = self._short_path_component_from_firmware_dirent(bytes(firmware))
        if name is None:
            return None
        return b"\\" + name + b"\\*.*\x00"

    def _service_first_root_directory_scan_probe_paused_locked(
        self, *, timeout: float = 3.0, max_hits: int = 768
    ) -> dict[str, object]:
        pattern = self._first_root_directory_scan_pattern_from_backing()
        if pattern is None:
            return {
                "event": "qemu-first-root-directory-scan-probe",
                "handled": False,
                "reason": "no-root-directory-pattern",
            }
        row = self._service_fs_scan_probe_paused_locked(
            timeout=timeout,
            max_hits=max_hits,
            pattern_bytes=pattern,
            event="qemu-first-root-directory-scan-probe",
        )
        row["handled"] = True
        return row

    def _first_child_directory_scan_pattern_from_backing(self) -> bytes | None:
        root = self._first_root_dirent_from_backing()
        if root is None:
            return None
        root_firmware = root.get("firmware")
        if not isinstance(root_firmware, (bytes, bytearray)) or len(root_firmware) < 0x20:
            return None
        if not (root_firmware[0x0B] & 0x10):
            return None
        root_name = self._short_path_component_from_firmware_dirent(bytes(root_firmware))
        if root_name is None:
            return None
        child = self._first_child_dirent_from_backing(root, require_directory=True)
        if child is None:
            return None
        child_firmware = child.get("firmware")
        if not isinstance(child_firmware, (bytes, bytearray)) or len(child_firmware) < 0x20:
            return None
        child_name = self._short_path_component_from_firmware_dirent(bytes(child_firmware))
        if child_name is None:
            return None
        return b"\\" + root_name + b"\\" + child_name + b"\\*.*\x00"

    def _service_first_child_directory_scan_probe_paused_locked(
        self, *, timeout: float = 3.0, max_hits: int = 1024
    ) -> dict[str, object]:
        pattern = self._first_child_directory_scan_pattern_from_backing()
        if pattern is None:
            return {
                "event": "qemu-first-child-directory-scan-probe",
                "handled": False,
                "reason": "no-child-directory-pattern",
            }
        row = self._service_fs_scan_probe_paused_locked(
            timeout=timeout,
            max_hits=max_hits,
            pattern_bytes=pattern,
            event="qemu-first-child-directory-scan-probe",
        )
        row["handled"] = True
        return row

    def _prepare_backing_file_path_probe_paused_locked(
        self,
        *,
        event: str,
        path_va: int = 0x80953A00,
        path: bytes | str | None = None,
    ) -> dict[str, object]:
        if self.config.machine.lower() == "bbk9588":
            return self._bbk9588_python_guest_service_disabled(event)
        file_path = self._find_path_from_backing(path) if path is not None else self._first_file_path_from_backing()
        if file_path is None:
            return {
                "event": event,
                "handled": False,
                "reason": "no-file-path",
            }
        path = file_path.get("path")
        if not isinstance(path, (bytes, bytearray)) or not path.endswith(b"\x00"):
            return {
                "event": event,
                "handled": False,
                "reason": "invalid-file-path",
            }
        row: dict[str, object] = {
            "event": event,
            "path": f"0x{path_va:08x}",
            "path_hex": bytes(path).hex(),
            "parts_hex": file_path.get("parts_hex", []),
            "cluster": f"0x{int(file_path.get('cluster', 0)):08x}",
            "size": f"0x{int(file_path.get('size', 0)):08x}",
        }
        if len(path) > 0x200 or not self._is_guest_ram_va(path_va, len(path)):
            row["handled"] = False
            row["reason"] = "path-buffer-outside-guest-ram"
            return row
        self._write_virtual_memory_paused_locked(path_va, bytes(path))
        self.gdb_write_count += 1
        row["handled"] = True
        row["path_va"] = path_va
        return row

    def _prepare_first_file_path_probe_paused_locked(self, *, event: str, path_va: int = 0x80953A00) -> dict[str, object]:
        return self._prepare_backing_file_path_probe_paused_locked(event=event, path_va=path_va)

    def _service_backing_file_open_probe_paused_locked(
        self,
        path: bytes | str,
        *,
        timeout: float = 3.0,
        max_hits: int = 1024,
        high_level: bool = False,
        path_va: int = 0x80953A00,
        event: str = "qemu-backing-file-open-probe",
    ) -> dict[str, object]:
        row = self._prepare_backing_file_path_probe_paused_locked(event=event, path=path, path_va=path_va)
        if not row.get("handled"):
            return row
        path_va = int(row.pop("path_va"))
        entry = 0x801717F4 if high_level else 0x801714EC
        row["entry"] = f"0x{entry:08x}"
        call = self._service_legacy_python_resource_hook_paused_locked(
            timeout=timeout,
            max_hits=max_hits,
            entry=entry,
            args=(path_va,),
            event=f"{event}-call",
        )
        row["call"] = call
        events = call.get("events") if isinstance(call.get("events"), list) else []
        row["hit_high_level_consumer"] = any(
            isinstance(event_row, dict) and event_row.get("pc") in {"0x801718b4", "0x801718bc"}
            for event_row in events
        )
        row["hit_low_level_consumer"] = any(
            isinstance(event_row, dict) and event_row.get("pc") in {"0x80171620", "0x801716fc"}
            for event_row in events
        )
        returned_v0 = None
        for event_row in reversed(events):
            if isinstance(event_row, dict) and event_row.get("kind") == "resource-pump-return":
                returned_v0 = event_row.get("v0")
                break
        row["returned_v0"] = returned_v0
        row["opened"] = returned_v0 not in (None, "0xffffffff")
        row["handled"] = True
        return row

    def _service_system_boot_file_probes_paused_locked(
        self, *, timeout_per_file: float = 4.0, max_hits_per_file: int = 4096
    ) -> dict[str, object]:
        row: dict[str, object] = {"event": "qemu-system-boot-file-probes", "files": []}
        entries = self._system_boot_file_entries_from_backing()
        row["path_count"] = len(entries)
        for entry in entries:
            path = entry.get("path")
            data = self._read_backing_file_bytes(entry)
            probe: dict[str, object] = {
                "event": "qemu-system-file-backing-read-probe",
                "handled": True,
                "path_hex": bytes(path).hex() if isinstance(path, (bytes, bytearray)) else None,
                "cluster": f"0x{int(entry.get('cluster', 0)):08x}",
                "size": f"0x{int(entry.get('size', 0)):08x}",
                "read": data is not None,
                "read_size": len(data) if data is not None else 0,
                "preview": data[:32].hex() if data is not None else None,
            }
            row["files"].append(probe)
            if not probe.get("read"):
                row["first_failed_path_hex"] = probe.get("path_hex")
                break
        row["read_count"] = sum(
            1 for item in row["files"] if isinstance(item, dict) and bool(item.get("read"))
        )
        row["handled"] = bool(row["files"])
        return row

    def _service_first_file_open_probe_paused_locked(
        self, *, timeout: float = 3.0, max_hits: int = 1024
    ) -> dict[str, object]:
        row = self._prepare_first_file_path_probe_paused_locked(event="qemu-first-file-open-probe")
        if not row.get("handled"):
            return row
        path_va = int(row.pop("path_va"))
        row["entry"] = "0x801714ec"
        call = self._service_legacy_python_resource_hook_paused_locked(
            timeout=timeout,
            max_hits=max_hits,
            entry=0x801714EC,
            args=(path_va,),
            event="qemu-first-file-open-probe-call",
        )
        row["call"] = call
        events = call.get("events") if isinstance(call.get("events"), list) else []
        row["hit_open_consumer"] = any(
            isinstance(event, dict)
            and event.get("pc") in {"0x80171620", "0x801716fc", "0x801718b4", "0x801718bc"}
            for event in events
        )
        row["handled"] = True
        return row

    def _service_first_file_high_level_open_probe_paused_locked(
        self, *, timeout: float = 3.0, max_hits: int = 1280
    ) -> dict[str, object]:
        row = self._prepare_first_file_path_probe_paused_locked(
            event="qemu-first-file-high-level-open-probe",
            path_va=0x80953C80,
        )
        if not row.get("handled"):
            return row
        path_va = int(row.pop("path_va"))
        row["entry"] = "0x801717f4"
        call = self._service_legacy_python_resource_hook_paused_locked(
            timeout=timeout,
            max_hits=max_hits,
            entry=0x801717F4,
            args=(path_va,),
            event="qemu-first-file-high-level-open-probe-call",
        )
        row["call"] = call
        events = call.get("events") if isinstance(call.get("events"), list) else []
        row["hit_high_level_consumer"] = any(
            isinstance(event, dict) and event.get("pc") in {"0x801718b4", "0x801718bc"} for event in events
        )
        row["hit_low_level_consumer"] = any(
            isinstance(event, dict) and event.get("pc") in {"0x80171620", "0x801716fc"} for event in events
        )
        row["handled"] = True
        return row

    def _prime_resource_refresh_paused_locked(self) -> dict[str, object] | None:
        if self.config.machine.lower() == "bbk9588":
            return None
        flags = self._read_u32_paused_locked(0x804BF440) or 0
        refresh = self._read_u8_paused_locked(0x804BF444) or 0
        fat_total = self._read_u32_paused_locked(0x80474240) or 0
        fat_mode = self._read_u8_paused_locked(0x8047428D) or 0
        if flags == 0 or refresh == 0:
            if flags == 0 and refresh == 0 and fat_total and fat_mode:
                self._write_u8_paused_locked(0x804BF444, 1)
                return {
                    "stage": "arm-resource-refresh-from-fat",
                    "flags_before": "0x00000000",
                    "refresh_before": "0x00",
                    "refresh_after": "0x01",
                    "fat_total_80474240": f"0x{fat_total:08x}",
                    "fat_mode_8047428d": f"0x{fat_mode:02x}",
                }
        return None

    def _task_node_trace_paused_locked(self, node_va: int) -> dict[str, object] | None:
        if node_va in (0, 1, 0xFFFFFFFF) or not self._is_guest_ram_va(node_va, 0x80):
            return None
        try:
            raw = self._read_virtual_memory_paused_locked(node_va, 0x80)
        except Exception:
            return None

        def u32(offset: int) -> int:
            return struct.unpack_from("<I", raw, offset)[0]

        def u8(offset: int) -> int:
            return raw[offset]

        name_raw = b""
        name_va = (node_va + 0x50) & 0xFFFFFFFF
        if self._is_guest_ram_va(name_va, 0x30):
            try:
                name_raw = self._read_virtual_memory_paused_locked(name_va, 0x30).split(b"\x00", 1)[0]
            except Exception:
                name_raw = b""
        name = ""
        if name_raw:
            for encoding in ("gb18030", "ascii", "latin1"):
                try:
                    name = name_raw.decode(encoding, errors="replace")
                    break
                except Exception:
                    continue
        return {
            "node": f"0x{node_va:08x}",
            "ctx_sp": f"0x{u32(0):08x}",
            "entry": f"0x{u32(0):08x}",
            "arg0": f"0x{u32(8):08x}",
            "flags34": f"0x{u8(0x34):02x}",
            "task_id35": f"0x{u8(0x35):02x}",
            "slot36": f"0x{u8(0x36):02x}",
            "name": name,
        }

    def _scheduled_task_context_paused_locked(self, task_id: int) -> dict[str, object]:
        task_id = int(task_id) & 0xFF
        table_va = 0x806C5D10
        node_va = self._read_u32_paused_locked(table_va + task_id * 4) or 0
        row: dict[str, object] = {
            "task_id": task_id,
            "node": self._format_u32(node_va),
            "ctx_sp": None,
            "target_pc": None,
            "pc_candidates": {},
            "task": None,
        }
        task = self._task_node_trace_paused_locked(node_va)
        if task is not None:
            row["task"] = task
        if node_va == 0 or not self._is_guest_ram_va(node_va, 4):
            row["error"] = "missing-task-node"
            return row
        ctx_sp = self._read_u32_paused_locked(node_va) or 0
        row["ctx_sp"] = self._format_u32(ctx_sp)
        if ctx_sp == 0 or not self._is_guest_ram_va((ctx_sp + 0x74) & 0xFFFFFFFF, 4):
            row["error"] = "missing-task-context"
            return row
        pc_candidates: dict[str, str | None] = {}
        for offset in range(0x40, 0x80, 4):
            value = self._read_u32_paused_locked((ctx_sp + offset) & 0xFFFFFFFF)
            if value is not None and (0x80000000 <= value <= 0x809FFFFF or value in (0, 0x20)):
                pc_candidates[f"ctx_{offset:02x}"] = self._format_u32(value)
        row["pc_candidates"] = pc_candidates
        target_pc = self._read_u32_paused_locked((ctx_sp + 0x70) & 0xFFFFFFFF) or 0
        row["target_pc"] = self._format_u32(target_pc)
        return row

    def _service_scheduled_fs_scan_task_paused_locked(
        self, task_id: int = 9, *, timeout: float = 2.0, max_hits: int = 512
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "event": "qemu-scheduled-fs-scan-task-service",
            "task_id": int(task_id),
            "handled": False,
        }
        if self.config.machine.lower() == "bbk9588":
            row.update(self._bbk9588_python_guest_service_disabled("qemu-scheduled-fs-scan-task-service"))
            return row
        context = self._scheduled_task_context_paused_locked(task_id)
        row["context"] = context
        scheduler = self._scheduler_dispatch_snapshot_paused_locked()
        row["scheduler"] = scheduler
        candidates = context.get("pc_candidates") if isinstance(context.get("pc_candidates"), dict) else {}
        has_fs_entry = context.get("target_pc") == "0x80173504" or "0x80173504" in set(candidates.values())
        scheduler_selected = scheduler.get("computed") is True and scheduler.get("next_task") == f"0x{int(task_id) & 0xFF:02x}"
        if not has_fs_entry and not scheduler_selected:
            row["reason"] = "task-context-is-not-fs-scan"
            return row
        row["trigger"] = "task-context-fs-entry" if has_fs_entry else "scheduler-selected-task"
        probe = self._service_fs_scan_probe_paused_locked(timeout=timeout, max_hits=max_hits)
        row["handled"] = True
        row["probe"] = probe
        row["native_fs_scan_fallback"] = probe.get("native_fs_scan_fallback")
        row["result_dirent"] = probe.get("result_dirent")
        return row

    def _handle_fs_dir_scan_branch_paused_locked(self, pc: int) -> dict[str, object]:
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "kind": "fs-dir-scan-branch",
            "handled": False,
        }
        if pc not in self._fs_branch_pcs():
            return row

        sp = self._read_register_paused_locked(29) & 0xFFFFFFFF
        s1 = self._read_register_paused_locked(17) & 0xFFFFFFFF
        s2 = self._read_register_paused_locked(18) & 0xFFFFFFFF
        s5 = self._read_register_paused_locked(21) & 0xFFFFFFFF
        s6 = self._read_register_paused_locked(22) & 0xFFFFFFFF
        v0 = self._read_register_paused_locked(2) & 0xFFFFFFFF
        v1 = self._read_register_paused_locked(3) & 0xFFFFFFFF
        a0 = self._read_register_paused_locked(4) & 0xFFFFFFFF
        self.gdb_register_read_count += 8

        row.update(
            {
                "sp": f"0x{sp:08x}",
                "s1": f"0x{s1:08x}",
                "s2": f"0x{s2:08x}",
                "s5": f"0x{s5:08x}",
                "s6": f"0x{s6:08x}",
                "v0": f"0x{v0:08x}",
                "v1": f"0x{v1:08x}",
                "a0": f"0x{a0:08x}",
            }
        )

        def read_stack_u32(offset: int) -> int:
            value = self._read_u32_paused_locked((sp + offset) & 0xFFFFFFFF)
            return 0 if value is None else value & 0xFFFFFFFF

        def finish(*writes: tuple[int, int], target: int, reason: str) -> dict[str, object]:
            for regno, value in writes:
                self._write_register_paused_locked(regno, value & 0xFFFFFFFF)
            self._write_register_paused_locked(37, target & 0xFFFFFFFF)
            self.gdb_register_write_count += len(writes) + 1
            row.update(
                {
                    "handled": True,
                    "reason": reason,
                    "return_pc": f"0x{target & 0xFFFFFFFF:08x}",
                }
            )
            return row

        if pc == 0x80173630:
            loaded_a0 = read_stack_u32(0x9C)
            row["loaded_a0"] = f"0x{loaded_a0:08x}"
            target = 0x80173F2C if s6 == s5 else 0x80173638
            return finish((4, loaded_a0), target=target, reason="loop-boundary")
        if pc == 0x80173640:
            target = 0x80173F14 if v1 != 0 else 0x80173648
            return finish((4, 0xE5), target=target, reason="deleted-or-empty-entry")
        if pc == 0x80173710:
            loaded_v0 = read_stack_u32(0x88)
            row["loaded_v0"] = f"0x{loaded_v0:08x}"
            target = 0x8017375C if v0 == v1 else 0x80173718
            return finish((2, loaded_v0), target=target, reason="name-compare")
        if pc == 0x80173768:
            target = 0x80173630 if v1 != 0 else 0x80173770
            return finish((17, (s1 + 0x20) & 0xFFFFFFFF), target=target, reason="advance-dirent")
        if pc == 0x80173F14:
            target = 0x8017375C if v1 == a0 else 0x80173F1C
            return finish((2, 0x2E), target=target, reason="dot-entry-compare")
        if pc == 0x80173F1C:
            next_s2 = (s2 + 0x20) & 0xFFFFFFFF
            target = 0x80173704 if v1 != v0 else 0x80173F24
            return finish((2, next_s2), target=target, reason="long-name-boundary")
        if pc == 0x80173F24:
            return finish((18, v0 & 0xFFFF), target=0x80173764, reason="set-name-offset")
        if pc == 0x80173F2C:
            loaded_v0 = read_stack_u32(0x90)
            row["loaded_v0"] = f"0x{loaded_v0:08x}"
            target = 0x80173638 if a0 == 0 else 0x80173F34
            return finish((2, loaded_v0), target=target, reason="root-scan-boundary")
        return row

    def _task_context_trace_row_paused_locked(self, pc: int) -> dict[str, object]:
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "kind": "task-context-restore" if pc == 0x800A7B40 else "task-context-switch-save",
            "save_current": pc == 0x800A7C18,
        }
        target_node = self._read_u32_paused_locked(0x80473F30) or 0
        current_node = self._read_u32_paused_locked(0x80473F50) or 0
        current_task = self._read_u8_paused_locked(0x80473F10) or 0
        last_task = self._read_u8_paused_locked(0x80473F11) or 0
        pending_count = self._read_u32_paused_locked(0x80473F1C) or 0
        sp = self._read_register_paused_locked(29) & 0xFFFFFFFF
        ra = self._read_register_paused_locked(31) & 0xFFFFFFFF
        target_ctx_sp = self._read_u32_paused_locked(target_node) if self._is_guest_ram_va(target_node, 4) else None
        target_pc = (
            self._read_u32_paused_locked((target_ctx_sp + 0x70) & 0xFFFFFFFF)
            if target_ctx_sp is not None and self._is_guest_ram_va((target_ctx_sp + 0x70) & 0xFFFFFFFF, 4)
            else None
        )
        row.update(
            {
                "target_node": f"0x{target_node:08x}",
                "current_node": f"0x{current_node:08x}",
                "current_task": f"0x{current_task:02x}",
                "last_task": f"0x{last_task:02x}",
                "pending_count": f"0x{pending_count:08x}",
                "sp": f"0x{sp:08x}",
                "ra": f"0x{ra:08x}",
                "target_ctx_sp": self._format_u32(target_ctx_sp),
                "target_pc": self._format_u32(target_pc),
                "target_task": self._task_node_trace_paused_locked(target_node),
                "current_task_node": self._task_node_trace_paused_locked(current_node),
            }
        )
        self.gdb_register_read_count += 2
        return row

    def _service_task_context_trace_paused_locked(self, *, timeout: float = 0.5, max_hits: int = 16) -> dict[str, object]:
        out: dict[str, object] = {
            "event": "qemu-task-context-trace-service",
            "breakpoints": ["0x800a7b40", "0x800a7c18"],
            "events": [],
        }
        if self.config.machine.lower() == "bbk9588":
            out.update(self._bbk9588_python_guest_service_disabled("qemu-task-context-trace-service"))
            out["handled_count"] = 0
            return out
        if self.gdb_sock is None:
            out["error"] = "QEMU GDB stub is not connected"
            return out
        inserted: list[int] = []
        active = {0x800A7B40, 0x800A7C18}
        deadline = time.time() + max(0.0, float(timeout))
        try:
            for addr in sorted(active):
                reply = _gdb_insert_breakpoint(self.gdb_sock, addr)
                if reply != "OK":
                    out["error"] = f"failed to insert breakpoint 0x{addr:08x}: {reply}"
                    return out
                inserted.append(addr)
            while len(out["events"]) < max(0, int(max_hits)) and time.time() < deadline:
                try:
                    stop = _gdb_continue_wait(self.gdb_sock, timeout=max(0.05, min(0.2, deadline - time.time())))
                except socket.timeout:
                    try:
                        _gdb_interrupt(self.gdb_sock)
                    except Exception:
                        pass
                    out["timeout"] = True
                    break
                pc = self._read_register_paused_locked(37) & 0xFFFFFFFF
                if pc not in active:
                    row: dict[str, object] = {
                        "pc": f"0x{pc:08x}",
                        "handled": False,
                        "reason": "unexpected-pc",
                        "stop": stop,
                    }
                    out["events"].append(row)
                    break
                row = self._task_context_trace_row_paused_locked(pc)
                row["handled"] = True
                row["stop"] = stop
                out["events"].append(row)
                self.task_context_trace_count += 1
                self.task_context_events.append(row)
                del self.task_context_events[:-128]

                _gdb_remove_breakpoint(self.gdb_sock, pc)
                if pc in inserted:
                    inserted.remove(pc)
                self._step_or_handle_resource_trace_branch_paused_locked(pc, row)
                if time.time() < deadline:
                    reply = _gdb_insert_breakpoint(self.gdb_sock, pc)
                    if reply == "OK":
                        inserted.append(pc)
                    else:
                        row["reinsert_error"] = reply
                        break
        finally:
            for addr in list(inserted):
                try:
                    _gdb_remove_breakpoint(self.gdb_sock, addr)
                except Exception:
                    pass
        out["handled_count"] = sum(1 for event in out["events"] if isinstance(event, dict) and event.get("handled"))
        out["final_pc"] = self._format_u32(self._read_pc_paused_locked())
        return out

    def service_task_context_trace(self, *, timeout: float = 0.5, max_hits: int = 16) -> dict[str, object]:
        return {
            "event": "qemu-task-context-trace-service",
            "disabled": True,
            "reason": "Python/GDB trace services were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }
    def _fs_trace_row_paused_locked(self, pc: int) -> dict[str, object]:
        sp = self._read_register_paused_locked(29) & 0xFFFFFFFF
        s1 = self._read_register_paused_locked(17) & 0xFFFFFFFF
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "v0": f"0x{self._read_register_paused_locked(2) & 0xFFFFFFFF:08x}",
            "a0": f"0x{self._read_register_paused_locked(4) & 0xFFFFFFFF:08x}",
            "a1": f"0x{self._read_register_paused_locked(5) & 0xFFFFFFFF:08x}",
            "s1": f"0x{s1:08x}",
            "s2": f"0x{self._read_register_paused_locked(18) & 0xFFFFFFFF:08x}",
            "s5": f"0x{self._read_register_paused_locked(21) & 0xFFFFFFFF:08x}",
            "s6": f"0x{self._read_register_paused_locked(22) & 0xFFFFFFFF:08x}",
            "sp": f"0x{sp:08x}",
            "ra": f"0x{self._read_register_paused_locked(31) & 0xFFFFFFFF:08x}",
            "sp90": self._format_u32(self._read_u32_paused_locked((sp + 0x90) & 0xFFFFFFFF)),
            "sp9c": self._format_u32(self._read_u32_paused_locked((sp + 0x9C) & 0xFFFFFFFF)),
            "dir0": None,
            "dir_attr": None,
            "dir_cluster": None,
            "dir_size": None,
            "dir_name_hex": None,
            "dirent_hex": None,
        }
        if self._is_guest_ram_va(s1, 0x20):
            try:
                data = self._read_virtual_memory_paused_locked(s1, 0x20)
                row["dir0"] = f"0x{data[0]:02x}"
                row["dir_attr"] = f"0x{data[0x0B]:02x}"
                row["dir_name_hex"] = data[:11].hex()
                row["dir_cluster"] = (
                    f"0x{((struct.unpack_from('<H', data, 0x14)[0] << 16) | struct.unpack_from('<H', data, 0x1A)[0]) & 0xFFFFFFFF:08x}"
                )
                row["dir_size"] = f"0x{struct.unpack_from('<I', data, 0x1C)[0]:08x}"
                row["dirent_hex"] = data.hex()
            except Exception as exc:
                row["dir_error"] = f"{type(exc).__name__}: {exc}"
        self.gdb_register_read_count += 9
        return row

    def _service_fs_trace_paused_locked(self, *, timeout: float = 0.5, max_hits: int = 32) -> dict[str, object]:
        pcs = self._fs_trace_pcs()
        out: dict[str, object] = {
            "event": "qemu-fs-trace-service",
            "breakpoints": [f"0x{pc:08x}" for pc in pcs],
            "events": [],
        }
        if self.config.machine.lower() == "bbk9588":
            out.update(self._bbk9588_python_guest_service_disabled("qemu-fs-trace-service"))
            out["handled_count"] = 0
            return out
        if self.gdb_sock is None:
            out["error"] = "QEMU GDB stub is not connected"
            return out
        active = set(pcs)
        inserted: list[int] = []
        deadline = time.time() + max(0.0, float(timeout))
        try:
            for addr in pcs:
                reply = _gdb_insert_breakpoint(self.gdb_sock, addr)
                if reply != "OK":
                    out["error"] = f"failed to insert breakpoint 0x{addr:08x}: {reply}"
                    return out
                inserted.append(addr)
            while len(out["events"]) < max(0, int(max_hits)) and time.time() < deadline:
                try:
                    stop = _gdb_continue_wait(self.gdb_sock, timeout=max(0.05, min(0.2, deadline - time.time())))
                except socket.timeout:
                    try:
                        _gdb_interrupt(self.gdb_sock)
                    except Exception:
                        pass
                    out["timeout"] = True
                    break
                pc = self._read_register_paused_locked(37) & 0xFFFFFFFF
                if pc not in active:
                    row: dict[str, object] = {
                        "pc": f"0x{pc:08x}",
                        "handled": False,
                        "reason": "unexpected-pc",
                        "stop": stop,
                    }
                    out["events"].append(row)
                    break
                row = self._fs_trace_row_paused_locked(pc)
                row["handled"] = True
                row["stop"] = stop
                branch = self._handle_fs_dir_scan_branch_paused_locked(pc)
                if branch.get("handled"):
                    row["branch"] = branch
                out["events"].append(row)
                self.fs_trace_count += 1
                self.fs_trace_events.append(row)
                del self.fs_trace_events[:-128]

                _gdb_remove_breakpoint(self.gdb_sock, pc)
                if pc in inserted:
                    inserted.remove(pc)
                step_stop = "branch-handled" if branch.get("handled") else self._step_paused_locked()
                row["step_stop"] = step_stop
                if time.time() < deadline:
                    reply = _gdb_insert_breakpoint(self.gdb_sock, pc)
                    if reply == "OK":
                        inserted.append(pc)
                    else:
                        row["reinsert_error"] = reply
                        break
        finally:
            for addr in list(inserted):
                try:
                    _gdb_remove_breakpoint(self.gdb_sock, addr)
                except Exception:
                    pass
        out["handled_count"] = sum(1 for event in out["events"] if isinstance(event, dict) and event.get("handled"))
        out["final_pc"] = self._format_u32(self._read_pc_paused_locked())
        return out

    def service_fs_trace(self, *, timeout: float = 0.5, max_hits: int = 32) -> dict[str, object]:
        return {
            "event": "qemu-fs-trace-service",
            "disabled": True,
            "reason": "Python/GDB trace services were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }
    def _event_loop_trace_row_paused_locked(self, pc: int) -> dict[str, object]:
        v0 = self._read_register_paused_locked(2) & 0xFFFFFFFF
        a0 = self._read_register_paused_locked(4) & 0xFFFFFFFF
        a1 = self._read_register_paused_locked(5) & 0xFFFFFFFF
        a2 = self._read_register_paused_locked(6) & 0xFFFFFFFF
        sp = self._read_register_paused_locked(29) & 0xFFFFFFFF
        ra = self._read_register_paused_locked(31) & 0xFFFFFFFF
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "v0": f"0x{v0:08x}",
            "a0": f"0x{a0:08x}",
            "a1": f"0x{a1:08x}",
            "a2": f"0x{a2:08x}",
            "sp": f"0x{sp:08x}",
            "ra": f"0x{ra:08x}",
            "event_record": None,
            "global_queue": self._format_u32(self._read_u32_paused_locked(0x80473F6C)),
        }
        record = v0 if pc == 0x8012CCFC else a0
        if self._is_guest_ram_va(record, 0x1C):
            try:
                data = self._read_virtual_memory_paused_locked(record, 0x1C)
                words = struct.unpack("<7I", data)
                row["event_record"] = {
                    "addr": f"0x{record:08x}",
                    "words": [f"0x{word:08x}" for word in words],
                    "object": f"0x{words[0]:08x}",
                    "event": f"0x{words[1]:08x}",
                    "arg0": f"0x{words[2]:08x}",
                    "arg1": f"0x{words[3]:08x}",
                }
            except Exception as exc:
                row["event_record_error"] = f"{type(exc).__name__}: {exc}"
        queue_obj = self._read_u32_paused_locked(0x80473F6C) or 0
        if self._is_guest_ram_va(queue_obj, 0x40):
            try:
                qdata = self._read_virtual_memory_paused_locked(queue_obj, 0x40)
                qwords = struct.unpack("<16I", qdata)
                row["queue_object_words"] = [f"0x{word:08x}" for word in qwords]
            except Exception:
                pass
        self.gdb_register_read_count += 6
        return row

    def _service_event_loop_trace_paused_locked(self, *, timeout: float = 0.5, max_hits: int = 32) -> dict[str, object]:
        pcs = (0x8000B25C, 0x8000B2C8, 0x8012CCF0, 0x8012CCFC)
        out: dict[str, object] = {
            "event": "qemu-event-loop-trace-service",
            "breakpoints": [f"0x{pc:08x}" for pc in pcs],
            "events": [],
        }
        if self.config.machine.lower() == "bbk9588":
            out.update(self._bbk9588_python_guest_service_disabled("qemu-event-loop-trace-service"))
            out["handled_count"] = 0
            return out
        if self.gdb_sock is None:
            out["error"] = "QEMU GDB stub is not connected"
            return out
        active = set(pcs)
        inserted: list[int] = []
        deadline = time.time() + max(0.0, float(timeout))
        try:
            for addr in pcs:
                reply = _gdb_insert_breakpoint(self.gdb_sock, addr)
                if reply != "OK":
                    out["error"] = f"failed to insert breakpoint 0x{addr:08x}: {reply}"
                    return out
                inserted.append(addr)
            while len(out["events"]) < max(0, int(max_hits)) and time.time() < deadline:
                try:
                    stop = _gdb_continue_wait(self.gdb_sock, timeout=max(0.05, min(0.2, deadline - time.time())))
                except socket.timeout:
                    try:
                        _gdb_interrupt(self.gdb_sock)
                    except Exception:
                        pass
                    out["timeout"] = True
                    break
                pc = self._read_register_paused_locked(37) & 0xFFFFFFFF
                if pc not in active:
                    row: dict[str, object] = {
                        "pc": f"0x{pc:08x}",
                        "handled": False,
                        "reason": "unexpected-pc",
                        "stop": stop,
                    }
                    out["events"].append(row)
                    break
                row = self._event_loop_trace_row_paused_locked(pc)
                row["handled"] = True
                row["stop"] = stop
                out["events"].append(row)
                self.event_loop_trace_count += 1
                self.event_loop_trace_events.append(row)
                del self.event_loop_trace_events[:-128]

                _gdb_remove_breakpoint(self.gdb_sock, pc)
                if pc in inserted:
                    inserted.remove(pc)
                step_stop = self._step_paused_locked()
                row["step_stop"] = step_stop
                if time.time() < deadline:
                    reply = _gdb_insert_breakpoint(self.gdb_sock, pc)
                    if reply == "OK":
                        inserted.append(pc)
                    else:
                        row["reinsert_error"] = reply
                        break
        finally:
            for addr in list(inserted):
                try:
                    _gdb_remove_breakpoint(self.gdb_sock, addr)
                except Exception:
                    pass
        out["handled_count"] = sum(1 for event in out["events"] if isinstance(event, dict) and event.get("handled"))
        out["final_pc"] = self._format_u32(self._read_pc_paused_locked())
        return out

    def service_event_loop_trace(self, *, timeout: float = 0.5, max_hits: int = 32) -> dict[str, object]:
        return {
            "event": "qemu-event-loop-trace-service",
            "disabled": True,
            "reason": "Python/GDB trace services were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }
    def _resource_trace_row_paused_locked(self, pc: int) -> dict[str, object]:
        regs = {
            "v0": self._read_register_paused_locked(2) & 0xFFFFFFFF,
            "a0": self._read_register_paused_locked(4) & 0xFFFFFFFF,
            "a1": self._read_register_paused_locked(5) & 0xFFFFFFFF,
            "a2": self._read_register_paused_locked(6) & 0xFFFFFFFF,
            "a3": self._read_register_paused_locked(7) & 0xFFFFFFFF,
            "s0": self._read_register_paused_locked(16) & 0xFFFFFFFF,
            "s1": self._read_register_paused_locked(17) & 0xFFFFFFFF,
            "s2": self._read_register_paused_locked(18) & 0xFFFFFFFF,
            "s3": self._read_register_paused_locked(19) & 0xFFFFFFFF,
            "s4": self._read_register_paused_locked(20) & 0xFFFFFFFF,
            "s5": self._read_register_paused_locked(21) & 0xFFFFFFFF,
            "s6": self._read_register_paused_locked(22) & 0xFFFFFFFF,
            "sp": self._read_register_paused_locked(29) & 0xFFFFFFFF,
            "ra": self._read_register_paused_locked(31) & 0xFFFFFFFF,
        }
        self.gdb_register_read_count += len(regs)
        globals_map = {
            "resource_state_804bf438": 0x804BF438,
            "resource_queue_804bf43c": 0x804BF43C,
            "resource_flags_804bf440": 0x804BF440,
            "resource_word_804bf444": 0x804BF444,
            "resource_word_804bf448": 0x804BF448,
            "resource_word_804bf44c": 0x804BF44C,
            "resource_word_804bf450": 0x804BF450,
            "resource_word_804bf454": 0x804BF454,
            "resource_word_804bf458": 0x804BF458,
            "resource_word_804bf45c": 0x804BF45C,
            "resource_word_804bf460": 0x804BF460,
            "resource_word_804bf464": 0x804BF464,
            "resource_word_804bf468": 0x804BF468,
            "resource_block_bytes_804bf480": 0x804BF480,
            "resource_word_804bf488": 0x804BF488,
            "desktop_resource_mgr_80478358": 0x80478358,
            "desktop_resource_count_8047835c": 0x8047835C,
            "fat_root_dir_sectors_80474234": 0x80474234,
            "fat_global_8047423c": 0x8047423C,
            "fat_global_80474240": 0x80474240,
            "fat_root_lba_80474244": 0x80474244,
            "fat_volume_lba_80474254": 0x80474254,
            "fat_fat_lba_80474260": 0x80474260,
            "fat_scan_mode_80474278": 0x80474278,
            "fat_scan_sector_bytes_8047427a": 0x8047427A,
            "fat_root_lba_8047429c": 0x8047429C,
            "fat_byte_8047428d": 0x8047428D,
            "fat_byte_8047428e": 0x8047428E,
        }
        bytes_map = {
            "resource_byte_804bf445": 0x804BF445,
            "fat_byte_8047428d": 0x8047428D,
            "fat_byte_8047428e": 0x8047428E,
        }
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "regs": {name: f"0x{value:08x}" for name, value in regs.items()},
            "globals": {name: self._format_u32(self._read_u32_paused_locked(addr)) for name, addr in globals_map.items()},
            "bytes": {
                name: (None if (value := self._read_u8_paused_locked(addr)) is None else f"0x{value & 0xFF:02x}")
                for name, addr in bytes_map.items()
            },
        }
        strings: dict[str, object] = {}
        for reg_name in ("a0", "s0", "s1", "s2", "s3"):
            value = regs[reg_name]
            if self._is_guest_ram_va(value, 1):
                strings[reg_name] = self._guest_c_string_trace_paused_locked(value, 0x80)
        if pc == 0x8001E8C0:
            previous_s0 = (regs["s0"] - 0x32) & 0xFFFFFFFF
            if self._is_guest_ram_va(previous_s0, 1):
                strings["s0_minus_0x32"] = self._guest_c_string_trace_paused_locked(previous_s0, 0x80)
        if strings:
            row["strings"] = strings
        s0 = regs["s0"]
        if self._is_guest_ram_va(s0, 0x58):
            try:
                data = self._read_virtual_memory_paused_locked(s0, 0x58)
                row["s0_object_words"] = [
                    f"0x{struct.unpack_from('<I', data, offset)[0]:08x}" for offset in range(0, 0x58, 4)
                ]
                row["s0_object_hex"] = data.hex()
            except Exception as exc:
                row["s0_object_error"] = f"{type(exc).__name__}: {exc}"
        for reg_name in ("s4", "s6"):
            value = regs[reg_name]
            if self._is_guest_ram_va(value, 0x40):
                try:
                    data = self._read_virtual_memory_paused_locked(value, 0x40)
                    row[f"{reg_name}_object_words"] = [
                        f"0x{struct.unpack_from('<I', data, offset)[0]:08x}" for offset in range(0, 0x40, 4)
                    ]
                    row[f"{reg_name}_object_hex"] = data.hex()
                except Exception as exc:
                    row[f"{reg_name}_object_error"] = f"{type(exc).__name__}: {exc}"
        return row

    def _handle_resource_trace_branch_paused_locked(self, pc: int) -> dict[str, object]:
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "kind": "resource-trace-branch",
            "handled": False,
        }
        if pc == 0x801754DC:
            data_va = self._read_register_paused_locked(19) & 0xFFFFFFFF
            self.gdb_register_read_count += 1
            row.update({"data": f"0x{data_va:08x}"})
            if not self._is_guest_ram_va(data_va, 3):
                row["reason"] = "invalid-resource-buffer"
                return row
            try:
                preview = self._read_virtual_memory_paused_locked(data_va, 3)
            except Exception as exc:
                row["reason"] = f"resource-buffer-read-failed: {type(exc).__name__}: {exc}"
                return row
            row["preview"] = preview.hex()
            if preview not in (b"DLX", b"\xE5LX", b"BM6", b"\xE5M6"):
                row["reason"] = "resource-header-not-ready"
                return row
            if preview == b"\xE5LX":
                self._write_u8_paused_locked(data_va, 0x44)
                self.gdb_write_count += 1
                row["restored_header"] = "DLX"
            elif preview == b"\xE5M6":
                self._write_u8_paused_locked(data_va, 0x42)
                self.gdb_write_count += 1
                row["restored_header"] = "BM"
            self._write_register_paused_locked(2, 1)
            self._write_register_paused_locked(37, 0x801754E4)
            self.gdb_register_write_count += 2
            row.update(
                {
                    "handled": True,
                    "reason": "desktop-resource-buffer-ready-at-callsite",
                    "return_pc": "0x801754e4",
                }
            )
            return row
        if pc == 0x801705EC:
            ra = self._read_register_paused_locked(31) & 0xFFFFFFFF
            data_va = self._read_register_paused_locked(19) & 0xFFFFFFFF
            self.gdb_register_read_count += 2
            row.update({"ra": f"0x{ra:08x}", "data": f"0x{data_va:08x}"})
            if ra != 0x801754E4:
                row["reason"] = "not-desktop-resource-check-return"
                return row
            if not self._is_guest_ram_va(data_va, 3):
                row["reason"] = "invalid-resource-buffer"
                return row
            try:
                preview = self._read_virtual_memory_paused_locked(data_va, 3)
            except Exception as exc:
                row["reason"] = f"resource-buffer-read-failed: {type(exc).__name__}: {exc}"
                return row
            row["preview"] = preview.hex()
            if preview not in (b"DLX", b"\xE5LX", b"BM6", b"\xE5M6"):
                row["reason"] = "resource-header-not-ready"
                return row
            if preview == b"\xE5LX":
                self._write_u8_paused_locked(data_va, 0x44)
                self.gdb_write_count += 1
                row["restored_header"] = "DLX"
            elif preview == b"\xE5M6":
                self._write_u8_paused_locked(data_va, 0x42)
                self.gdb_write_count += 1
                row["restored_header"] = "BM"
            self._write_register_paused_locked(2, 1)
            self._write_register_paused_locked(37, ra)
            self.gdb_register_write_count += 2
            row.update(
                {
                    "handled": True,
                    "reason": "desktop-resource-buffer-ready",
                    "return_pc": f"0x{ra:08x}",
                }
            )
            return row
        if pc == 0x80173928:
            v0 = self._read_register_paused_locked(2) & 0xFFFFFFFF
            sp = self._read_register_paused_locked(29) & 0xFFFFFFFF
            dirent_va = self._read_register_paused_locked(4) & 0xFFFFFFFF
            stacked_s4 = self._read_u32_paused_locked((sp + 0x7C) & 0xFFFFFFFF) or 0
            s4 = stacked_s4
            synced_dir_cluster = False
            if v0 == 0x10 and self._is_guest_ram_va(dirent_va, 0x20):
                dirent = self._read_virtual_memory_paused_locked(dirent_va, 0x20)
                firmware = self._firmware_dirent_from_fat_dirent(dirent)
                dir_cluster = struct.unpack_from("<I", firmware, 0x14)[0]
                if dir_cluster >= 2:
                    s4 = dir_cluster
                    synced_dir_cluster = dir_cluster != stacked_s4
                    self._write_u32_paused_locked((sp + 0x7C) & 0xFFFFFFFF, dir_cluster)
            target = 0x80173E84 if v0 == 0 else 0x80173930
            self._write_register_paused_locked(20, s4)
            self._write_register_paused_locked(37, target)
            self.gdb_register_read_count += 3
            self.gdb_register_write_count += 2
            if synced_dir_cluster:
                self.gdb_write_count += 1
            row.update(
                {
                    "handled": True,
                    "reason": "dirent-attribute-branch",
                    "v0": f"0x{v0:08x}",
                    "sp": f"0x{sp:08x}",
                    "dirent": f"0x{dirent_va:08x}",
                    "stacked_s4": f"0x{stacked_s4:08x}",
                    "loaded_s4": f"0x{s4:08x}",
                    "synced_dir_cluster": synced_dir_cluster,
                    "return_pc": f"0x{target:08x}",
                }
            )
        return row

    def _step_or_handle_resource_trace_branch_paused_locked(self, pc: int, row: dict[str, object]) -> None:
        row["resource_trace_hooks_disabled"] = True
        row["step_stop"] = self._step_paused_locked()

    def _service_resource_trace_paused_locked(self, *, timeout: float = 0.5, max_hits: int = 32) -> dict[str, object]:
        return {
            "event": "qemu-resource-trace-service",
            "disabled": True,
            "reason": "Python/GDB resource trace services were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }

    def service_resource_trace(self, *, timeout: float = 0.5, max_hits: int = 32) -> dict[str, object]:
        return {
            "event": "qemu-resource-trace-service",
            "disabled": True,
            "reason": "Python/GDB resource trace services were removed from the hardware-model path",
            "handled_count": 0,
            "events": [],
        }

    @staticmethod
    def _format_u32(value: int | None) -> str | None:
        return None if value is None else f"0x{value & 0xFFFFFFFF:08x}"

    def _read_u32_paused_locked(self, va: int) -> int | None:
        try:
            return struct.unpack("<I", self._read_virtual_memory_paused_locked(va, 4))[0]
        except Exception:
            return None

    def _read_u8_paused_locked(self, va: int) -> int | None:
        try:
            return self._read_virtual_memory_paused_locked(va, 1)[0]
        except Exception:
            return None

    def _read_u16_paused_locked(self, va: int) -> int | None:
        try:
            return struct.unpack("<H", self._read_virtual_memory_paused_locked(va, 2))[0]
        except Exception:
            return None

    def _write_u8_paused_locked(self, va: int, value: int) -> None:
        self._write_virtual_memory_paused_locked(va, bytes([value & 0xFF]))

    def _write_u16_paused_locked(self, va: int, value: int) -> None:
        self._write_virtual_memory_paused_locked(va, struct.pack("<H", value & 0xFFFF))

    def _write_u32_paused_locked(self, va: int, value: int) -> None:
        self._write_virtual_memory_paused_locked(va, struct.pack("<I", value & 0xFFFFFFFF))

    @staticmethod
    def _touch_panel_to_adc(x: int, y: int) -> tuple[int, int]:
        panel_x = max(0, min(239, int(x)))
        panel_y = max(0, min(319, int(y)))
        (x0, y0, raw_x00, raw_y00), (x1, _y0b, raw_x10, raw_y10), (
            _x1b,
            y1,
            raw_x11,
            raw_y11,
        ), (_x0b, _y1b, raw_x01, raw_y01) = TOUCH_CALIBRATION_REFERENCE_POINTS
        tx = (panel_x - x0) / max(1, x1 - x0)
        ty = (panel_y - y0) / max(1, y1 - y0)
        raw_x_top = raw_x00 + (raw_x10 - raw_x00) * tx
        raw_x_bottom = raw_x01 + (raw_x11 - raw_x01) * tx
        raw_y_top = raw_y00 + (raw_y10 - raw_y00) * tx
        raw_y_bottom = raw_y01 + (raw_y11 - raw_y01) * tx
        raw_x = round(raw_x_top + (raw_x_bottom - raw_x_top) * ty)
        raw_y = round(raw_y_top + (raw_y_bottom - raw_y_top) * ty)
        return max(0, min(0xFFF, raw_x)), max(0, min(0xFFF, raw_y))

    def guest_queue_snapshot(self, global_va: int = 0x80473F6C) -> dict[str, object]:
        """Snapshot the firmware queue object rooted at a guest global pointer."""

        out: dict[str, object] = {
            "global_addr": f"0x{global_va & 0xFFFFFFFF:08x}",
            "global_value": None,
        }
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                out["error"] = "QEMU process is not running"
                return out
            if self.gdb_sock is None:
                out["error"] = "QEMU GDB stub is not connected"
                return out
            try:
                self._pause_for_gdb_locked()
                obj = self._read_u32_paused_locked(global_va)
                out["global_value"] = self._format_u32(obj)
                out["global_80473f6c"] = self._format_u32(obj)
                self.gdb_read_count += 1
                self.last_gdb_error = None
                if obj is None or not self._is_guest_ram_va(obj, 0x10):
                    return out

                obj_data = self._read_virtual_memory_paused_locked(obj, 0x40)
                obj_words = [struct.unpack_from("<I", obj_data, i)[0] for i in range(0, 0x40, 4)]
                queue = obj_words[2]
                out["object_words"] = [f"0x{word:08x}" for word in obj_words]
                out["object_type_byte"] = f"0x{obj_data[0]:02x}"
                out["queue_ptr"] = f"0x{queue:08x}"
                if not self._is_guest_ram_va(queue, 0x20):
                    return out

                q_data = self._read_virtual_memory_paused_locked(queue, 0x40)
                q_words = [struct.unpack_from("<I", q_data, i)[0] for i in range(0, 0x40, 4)]
                out["queue_words"] = [f"0x{word:08x}" for word in q_words]
                ring_start = q_words[1]
                ring_end = q_words[2]
                read_ptr = q_words[4]
                count = q_words[6]
                out["queue_fields"] = {
                    "ring_start_04": f"0x{ring_start:08x}",
                    "ring_end_08": f"0x{ring_end:08x}",
                    "read_ptr_10": f"0x{read_ptr:08x}",
                    "count_18": f"0x{count:08x}",
                }
                entries: list[dict[str, object]] = []
                if self._is_guest_ram_va(ring_start, 4) and self._is_guest_ram_va(ring_end, 4):
                    max_entries = min(16, max(0, (ring_end - ring_start) // 4))
                    for idx in range(max_entries):
                        entry_va = ring_start + idx * 4
                        value = self._read_u32_paused_locked(entry_va)
                        entries.append(
                            {
                                "index": idx,
                                "addr": f"0x{entry_va:08x}",
                                "value": self._format_u32(value),
                                "is_read_ptr": entry_va == read_ptr,
                            }
                        )
                out["ring_entries"] = entries
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                out["error"] = self.last_gdb_error
                return out
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_display_queue_snapshot(self, queue_va: int = 0x80825840) -> dict[str, object]:
        """Snapshot the GUI/display event ring used by the firmware input path."""

        out: dict[str, object] = {"queue_va": f"0x{queue_va & 0xFFFFFFFF:08x}"}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                out["error"] = "QEMU process is not running"
                return out
            if self.gdb_sock is None:
                out["error"] = "QEMU GDB stub is not connected"
                return out
            try:
                self._pause_for_gdb_locked()
                if not self._is_guest_ram_va(queue_va, 0x20):
                    out["mapped"] = False
                    return out
                out["mapped"] = True
                data = self._read_virtual_memory_paused_locked(queue_va, 0x20)
                words = [struct.unpack_from("<I", data, off)[0] for off in range(0, 0x20, 4)]
                out["words_00_1c"] = [f"0x{word:08x}" for word in words]
                buffer_va = words[4]
                capacity = words[5]
                read_index = words[6]
                write_index = words[7]
                out.update(
                    {
                        "buffer_va": f"0x{buffer_va:08x}",
                        "capacity": capacity,
                        "read_index": read_index,
                        "write_index": write_index,
                    }
                )
                entries = []
                max_entries = min(capacity, 16)
                if buffer_va and capacity and self._is_guest_ram_va(buffer_va, max_entries * 0x1C):
                    for idx in range(max_entries):
                        entry_va = buffer_va + idx * 0x1C
                        entry_data = self._read_virtual_memory_paused_locked(entry_va, 0x1C)
                        entry_words = [struct.unpack_from("<I", entry_data, off)[0] for off in range(0, 0x1C, 4)]
                        entries.append(
                            {
                                "index": idx,
                                "va": f"0x{entry_va:08x}",
                                "words": [f"0x{word:08x}" for word in entry_words],
                            }
                        )
                out["entries"] = entries
                self.gdb_read_count += 1
                self.last_gdb_error = None
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                out["error"] = self.last_gdb_error
                return out
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def dmac_trace_snapshot(self) -> dict[str, object]:
        """Read the compact DMAC state exported by the QEMU machine model."""

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"available": False, "reason": "QEMU process is not running"}
            try:
                raw = self._read_guest_ram_snapshot_locked(
                    DEFAULT_QEMU_DMAC_TRACE,
                    16 * 4,
                )
                words = struct.unpack("<16I", raw)
            except Exception as exc:
                return {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        if words[0] != QEMU_DMAC_TRACE_MAGIC:
            return {
                "available": False,
                "magic": f"0x{words[0]:08x}",
                "reason": "DMAC trace has not been initialized",
            }
        event_names = {
            1: "register-write",
            3: "transfer-complete",
            4: "auto-ram-complete",
            5: "descriptor-fetch",
            6: "audio-partial",
        }
        return {
            "available": True,
            "seq": words[1],
            "event": words[2],
            "event_name": event_names.get(words[2], "unknown"),
            "channel": words[3],
            "offset": f"0x{words[4]:08x}",
            "value": f"0x{words[5]:08x}",
            "pc": f"0x{words[6]:08x}",
            "intc_pending": f"0x{words[7]:08x}",
            "intc_mask": f"0x{words[8]:08x}",
            "dirqp": f"0x{words[9]:08x}",
            "channel2": {
                "status": f"0x{words[10]:08x}",
                "config": f"0x{words[11]:08x}",
                "count": words[12],
            },
            "channel3": {
                "status": f"0x{words[13]:08x}",
                "config": f"0x{words[14]:08x}",
                "count": words[15],
            },
        }

    def guest_gui_state_snapshot(self) -> dict[str, object]:
        """Read GUI/touch globals that indicate whether C200 reached interactive UI state."""

        fields = {
            "active_object_80474048": (0x80474048, 4),
            "active_object_8047404c": (0x8047404C, 4),
            "active_object_80474050": (0x80474050, 4),
            "gui_busy_count_80825800": (0x80825800, 4),
            "gui_busy_head_80825808": (0x80825808, 4),
            "gui_busy_count_80825820": (0x80825820, 4),
            "gui_busy_flags_80825824": (0x80825824, 4),
            "gui_busy_head_80825828": (0x80825828, 4),
            "gui_busy_tail_8082582c": (0x8082582C, 4),
            "gui_busy_active_node_80825830": (0x80825830, 4),
            "gui_busy_active_object_80825834": (0x80825834, 4),
            "gui_queue_80825840": (0x80825840, 4),
            "gui_callback_8082584c": (0x8082584C, 4),
            "gui_ring_80825850": (0x80825850, 4),
            "gui_scan_active_804a6994": (0x804A6994, 1),
            "gui_scan_index_804a6995": (0x804A6995, 1),
            "modal_804a65b0": (0x804A65B0, 4),
            "modal_804a65b4": (0x804A65B4, 4),
            "modal_804a65c0": (0x804A65C0, 4),
            "modal_804a65c4": (0x804A65C4, 4),
            "desktop_resource_mgr_80478358": (0x80478358, 4),
            "desktop_resource_count_8047835c": (0x8047835C, 4),
            "desktop_resource_trace_magic_807f7e80": (0x807F7E80, 4),
            "desktop_resource_trace_pc_807f7e84": (0x807F7E84, 4),
            "desktop_resource_trace_path_807f7e88": (0x807F7E88, 4),
            "desktop_resource_trace_result_807f7e8c": (0x807F7E8C, 4),
            "desktop_resource_trace_match_807f7e90": (0x807F7E90, 4),
            "desktop_resource_trace_count_807f7e94": (0x807F7E94, 4),
            "desktop_resource_trace_path0_807f7e98": (0x807F7E98, 4),
            "desktop_resource_trace_path1_807f7e9c": (0x807F7E9C, 4),
            "desktop_resource_trace_path2_807f7ea0": (0x807F7EA0, 4),
            "desktop_resource_trace_path3_807f7ea4": (0x807F7EA4, 4),
            "desktop_resource_trace_path4_807f7ea8": (0x807F7EA8, 4),
            "desktop_resource_trace_path5_807f7eac": (0x807F7EAC, 4),
            "desktop_resource_trace_path6_807f7eb0": (0x807F7EB0, 4),
            "desktop_resource_trace_path7_807f7eb4": (0x807F7EB4, 4),
            "gui_active_fallback_807f7ec0": (0x807F7EC0, 4),
            "window_pool_804a65e8": (0x804A65E8, 4),
            "window_pool_804a65f8": (0x804A65F8, 4),
            "input_gate_80473f08": (0x80473F08, 1),
            "input_group_80473f38": (0x80473F38, 1),
            "input_byte0_80473f40": (0x80473F40, 1),
            "input_byte1_80473f41": (0x80473F41, 1),
            "input_byte2_80473f42": (0x80473F42, 1),
            "input_byte3_80473f43": (0x80473F43, 1),
            "input_byte4_80473f44": (0x80473F44, 1),
            "input_byte5_80473f45": (0x80473F45, 1),
            "input_byte6_80473f46": (0x80473F46, 1),
            "input_byte7_80473f47": (0x80473F47, 1),
            "touch_mode_flag_8048daf4": (0x8048DAF4, 4),
            "touch_flag_8048dd00": (0x8048DD00, 4),
            "touch_flag_8048dd04": (0x8048DD04, 4),
            "touch_flag_8048dd08": (0x8048DD08, 4),
            "touch_x_80370fc8": (0x80370FC8, 4),
            "touch_y_80370fcc": (0x80370FCC, 4),
            "touch_latch_down_807f7110": (0x807F7110, 1),
            "touch_latch_raw_x_807f7112": (0x807F7112, 2),
            "touch_latch_raw_y_807f7114": (0x807F7114, 2),
            "touch_latch_x_807f7116": (0x807F7116, 2),
            "touch_latch_y_807f7118": (0x807F7118, 2),
            "fs_volume_count_80474254": (0x80474254, 1),
            "fs_cluster_base_80474260": (0x80474260, 4),
            "fs_cluster_limit_80474264": (0x80474264, 4),
            "fs_default_sector_8047429c": (0x8047429C, 4),
            "cluster_trace_magic_807f7c00": (0x807F7C00, 4),
            "cluster_trace_count_807f7c04": (0x807F7C04, 4),
            "cluster_trace_cluster_807f7c08": (0x807F7C08, 4),
            "cluster_trace_buffer_807f7c0c": (0x807F7C0C, 4),
            "cluster_trace_return_807f7c10": (0x807F7C10, 4),
            "cluster_trace_status_807f7c14": (0x807F7C14, 4),
            "cluster_trace_sectors_807f7c18": (0x807F7C18, 4),
            "cluster_trace_length_807f7c1c": (0x807F7C1C, 4),
            "cluster_trace_detail_reason_807f7c20": (0x807F7C20, 4),
            "cluster_trace_detail_arg0_807f7c24": (0x807F7C24, 4),
            "cluster_trace_detail_arg1_807f7c28": (0x807F7C28, 4),
            "cluster_trace_call_state_807f7c20": (0x807F7C20, 4),
            "cluster_trace_call_cluster_807f7c24": (0x807F7C24, 4),
            "cluster_trace_call_arg_807f7c28": (0x807F7C28, 4),
            "resource_cache_enabled_804bf434": (0x804BF434, 4),
            "resource_cache_base_8086d180": (0x8086D180, 4),
            "resource_cache_age_8086d188": (0x8086D188, 4),
            "scheduler_run_enabled_80473f09": (0x80473F09, 1),
            "scheduler_countdown_80473f08": (0x80473F08, 1),
            "scheduler_current_task_80473f10": (0x80473F10, 1),
            "scheduler_last_task_80473f11": (0x80473F11, 1),
            "scheduler_pending_count_80473f1c": (0x80473F1C, 4),
            "scheduler_active_node_80473f30": (0x80473F30, 4),
            "scheduler_dispatch_delay_80473f4d": (0x80473F4D, 1),
            "scheduler_current_node_80473f50": (0x80473F50, 4),
            "irq24_signal_node_80473f78": (0x80473F78, 4),
            "irq25_signal_node_80473f7c": (0x80473F7C, 4),
            "irq20_handler_80474724": (0x80474684 + 20 * 8, 4),
            "irq20_arg_80474728": (0x80474684 + 20 * 8 + 4, 4),
            "irq21_handler_8047472c": (0x80474684 + 21 * 8, 4),
            "irq21_arg_80474730": (0x80474684 + 21 * 8 + 4, 4),
            "irq22_handler_80474734": (0x80474684 + 22 * 8, 4),
            "irq22_arg_80474738": (0x80474684 + 22 * 8 + 4, 4),
            "irq23_handler_8047473c": (0x80474684 + 23 * 8, 4),
            "irq23_arg_80474740": (0x80474684 + 23 * 8 + 4, 4),
            "irq24_handler_80474744": (0x80474684 + 24 * 8, 4),
            "irq24_arg_80474748": (0x80474684 + 24 * 8 + 4, 4),
            "irq25_handler_8047474c": (0x80474684 + 25 * 8, 4),
            "irq25_arg_80474750": (0x80474684 + 25 * 8 + 4, 4),
            "irq26_handler_80474754": (0x80474684 + 26 * 8, 4),
            "irq26_arg_80474758": (0x80474684 + 26 * 8 + 4, 4),
            "irq27_handler_8047475c": (0x80474684 + 27 * 8, 4),
            "irq27_arg_80474760": (0x80474684 + 27 * 8 + 4, 4),
        }
        out: dict[str, object] = {}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                for name, (addr, size) in fields.items():
                    data = self._read_guest_ram_snapshot_locked(addr, size)
                    value = int.from_bytes(data, "little")
                    out[name] = f"0x{value:0{size * 2}x}"
                active = int(str(out.get("active_object_80474048", "0x0")), 16)
                out["active_object_ready"] = bool(active and self._is_guest_ram_va(active, 0x90))
                for pointer_name in (
                    "active_object_80474048",
                    "active_object_8047404c",
                    "active_object_80474050",
                    "gui_busy_head_80825808",
                    "gui_busy_head_80825828",
                    "gui_busy_tail_8082582c",
                    "gui_busy_active_node_80825830",
                    "gui_busy_active_object_80825834",
                    "gui_ring_80825850",
                    "modal_804a65c0",
                    "desktop_resource_mgr_80478358",
                    "scheduler_active_node_80473f30",
                    "scheduler_current_node_80473f50",
                    "irq24_signal_node_80473f78",
                    "irq25_signal_node_80473f7c",
                ):
                    ptr = int(str(out.get(pointer_name, "0x0")), 16)
                    if ptr and self._is_guest_ram_va(ptr, 0x40):
                        block = self._read_guest_ram_snapshot_locked(ptr, 0x40)
                        words = [struct.unpack_from("<I", block, off)[0] for off in range(0, 0x40, 4)]
                        out[f"{pointer_name}_words_00_3c"] = [f"0x{word:08x}" for word in words]
                busy_nodes: list[dict[str, object]] = []
                busy = int(str(out.get("gui_busy_head_80825828", "0x0")), 16)
                for index in range(8):
                    if not busy or not self._is_guest_ram_va(busy, 0x80):
                        break
                    block = self._read_guest_ram_snapshot_locked(busy, 0x80)
                    words = [struct.unpack_from("<I", block, off)[0] for off in range(0, 0x80, 4)]
                    busy_nodes.append(
                        {
                            "index": index,
                            "addr": f"0x{busy:08x}",
                            "next": f"0x{words[1]:08x}",
                            "words_00_7c": [f"0x{word:08x}" for word in words],
                            "ascii_18_2f": "".join(
                                chr(ch) if 32 <= ch < 127 else "."
                                for ch in block[0x18:0x30]
                            ),
                        }
                    )
                    next_busy = words[1]
                    if next_busy == busy:
                        break
                    busy = next_busy
                out["gui_busy_nodes_80825828"] = busy_nodes
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_scheduler_state_snapshot(self) -> dict[str, object]:
        """Read-only snapshot of firmware scheduler, IRQ table, and INTC state."""

        out: dict[str, object] = {}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            if self.gdb_sock is None:
                return {"error": "QEMU GDB stub is not connected"}
            try:
                self._pause_for_gdb_locked()

                sched_base = 0x80473F08
                sched = self._read_virtual_memory_paused_locked(sched_base, 0x60)
                sched_words = [struct.unpack_from("<I", sched, off)[0] for off in range(0, 0x60, 4)]
                out["scheduler_addr"] = f"0x{sched_base:08x}"
                out["scheduler_bytes_00_5f"] = sched.hex()
                out["scheduler_words_00_5c"] = [f"0x{word:08x}" for word in sched_words]
                out["scheduler_fields"] = {
                    "countdown_3f08": f"0x{sched[0]:02x}",
                    "run_enabled_3f09": f"0x{sched[1]:02x}",
                    "current_task_3f10": f"0x{sched[8]:02x}",
                    "last_task_3f11": f"0x{sched[9]:02x}",
                    "group_mask_3f38": f"0x{sched[0x30]:02x}",
                    "ready_group0_3f40": f"0x{sched[0x38]:02x}",
                    "ready_group1_3f41": f"0x{sched[0x39]:02x}",
                    "dispatch_delay_3f4d": f"0x{sched[0x45]:02x}",
                    "current_node_3f50": f"0x{sched_words[0x48 // 4]:08x}",
                }

                task_table_va = 0x806C5D10
                task_words: list[int] = []
                try:
                    task_table = self._read_virtual_memory_paused_locked(task_table_va, 0x100)
                    task_words = [struct.unpack_from("<I", task_table, off)[0] for off in range(0, 0x100, 4)]
                    out["task_table_806c5d10"] = [f"0x{word:08x}" for word in task_words]
                except Exception as exc:
                    out["task_table_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    priority = self._read_virtual_memory_paused_locked(0x8024A990, 0x110)
                    out["scheduler_priority_8024a990"] = [f"0x{value:02x}" for value in priority]
                    group_mask = sched[0x30]
                    group_index = priority[0x08 + group_mask]
                    ready = sched[0x38 + group_index] if group_index < 8 else 0
                    ready_index = priority[0x08 + ready]
                    selected_task = (group_index * 8 + ready_index) & 0xff
                    out["scheduler_selection"] = {
                        "group_mask": f"0x{group_mask:02x}",
                        "group_index": f"0x{group_index:02x}",
                        "ready_byte": f"0x{ready:02x}",
                        "ready_index": f"0x{ready_index:02x}",
                        "task": f"0x{selected_task:02x}",
                    }
                except Exception as exc:
                    out["scheduler_priority_error"] = f"{type(exc).__name__}: {exc}"
                task9 = task_words[9] if len(task_words) > 9 else 0
                out["task9_node"] = f"0x{task9:08x}"
                out["task9_node_valid"] = bool(self._is_guest_ram_va(task9, 0x80))
                if self._is_guest_ram_va(task9, 0x80):
                    node = self._read_virtual_memory_paused_locked(task9, 0x80)
                    node_words = [struct.unpack_from("<I", node, off)[0] for off in range(0, 0x80, 4)]
                    out["task9_node_words_00_7c"] = [f"0x{word:08x}" for word in node_words]

                irq_entries: dict[str, object] = {}
                for irq in (12, 15, 22, 23, 24, 25, 26, 27, 30):
                    entry_va = 0x80474684 + irq * 8
                    handler = self._read_u32_paused_locked(entry_va) or 0
                    arg = self._read_u32_paused_locked(entry_va + 4) or 0
                    irq_entries[str(irq)] = {
                        "entry": f"0x{entry_va:08x}",
                        "handler": f"0x{handler:08x}",
                        "arg": f"0x{arg:08x}",
                    }
                out["irq_table"] = irq_entries

                intc_words: dict[str, object] = {}
                for offset in range(0, 0x24, 4):
                    va = 0xB0001000 + offset
                    value = self._read_u32_paused_locked(va)
                    intc_words[f"0x{va:08x}"] = self._format_u32(value)
                out["intc_regs_b0001000"] = intc_words
                tcu_words: dict[str, object] = {}
                for offset in (0x28, 0x38, 0x3C, 0x50, 0x54):
                    va = 0xB0002000 + offset
                    value = self._read_u32_paused_locked(va)
                    tcu_words[f"0x{va:08x}"] = self._format_u32(value)
                out["tcu_regs_b0002000"] = tcu_words
                try:
                    cp0_status = self._read_register_paused_locked(32)
                    cp0_cause = self._read_register_paused_locked(33)
                    pc = self._read_register_paused_locked(37)
                    self.gdb_register_read_count += 3
                    out["cp0"] = {
                        "status": f"0x{cp0_status & 0xFFFFFFFF:08x}",
                        "cause": f"0x{cp0_cause & 0xFFFFFFFF:08x}",
                        "pc": f"0x{pc & 0xFFFFFFFF:08x}",
                    }
                except Exception as exc:
                    out["cp0_error"] = f"{type(exc).__name__}: {exc}"
                self.gdb_read_count += 1
                self.last_gdb_error = None
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_touch_device_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 C touch/SADC diagnostic mirror."""

        if (
            self.config.machine.lower() == "bbk9588"
            and not self._bbk_machine_bool_option_enabled("touch-trace")
        ):
            return {
                "available": False,
                "disabled": True,
                "reason": "touch-trace machine option is disabled",
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
            "sadc_control",
            "sadc_last_read_offset",
            "sadc_last_read_value",
            "sadc_last_write_offset",
            "sadc_last_write_value",
            "gpio_last_read_offset",
            "gpio_last_read_value",
            "gpio_last_flag_offset",
            "gpio_last_flag_value",
            "intc_mask",
            "tcu_enabled_mask",
            "tcu_pending_mask",
            "tcu_compare0",
            "tcu_compare1",
            "tcu_period0_ms",
            "tcu_period1_ms",
            "tcu_deadline0_ms_low",
            "tcu_deadline1_ms_low",
            "intc_last_unmasked_pending",
            "intc_last_level",
            "intc_update_count",
            "intc_last_cp0_status",
            "intc_last_cp0_cause",
            "cpu_irq_ip2_level",
            "cpu_interrupt_request",
            "intc_last_read_offset",
            "intc_last_read_value",
            "intc_last_write_offset",
            "intc_last_write_value",
            "intc_ack_count",
            "intc_ack_tcu_count",
            "tcu_last_read_offset",
            "tcu_last_read_value",
            "tcu_last_write_offset",
            "tcu_last_write_value",
            "tcu_irq_raise_count",
            "reserved_c4",
            "msc_read_pending",
            "msc_write_pending",
            "msc_data_ready",
            "msc_read_lba",
            "msc_write_lba",
            "msc_dma_complete_count",
            "msc_last_cmd",
            "msc_last_arg",
            "msc_last_dma_phys",
            "msc_last_dma_words",
            "nand_ready_raise_count",
            "nand_page_read_count",
            "nand_program_count",
            "nand_erase_count",
            "nand_last_cmd",
            "nand_last_page",
            "nand_last_column",
            "nand_last_block",
            "nand_busy_reads",
            "nand_bch_busy_reads",
            "nand_addr_count",
            "gpio_flag_200",
            "cpm_clkgr_wake_mask",
            "cpm_scr_wake_mask",
            "reserved_128",
            "reserved_12c",
            "reserved_130",
            "tcu_irq_mask",
            "tcu_compare4",
            "tcu_period4_ms",
            "tcu_deadline4_ms_low",
        )
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                data = self._read_guest_ram_snapshot_locked(DEFAULT_QEMU_TOUCH_TRACE, 0x144)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, 0x144, 4)
                ]
                out: dict[str, object] = {
                    label: f"0x{value:08x}" for label, value in zip(labels, words)
                }
                out["available"] = words[0] == QEMU_TOUCH_TRACE_MAGIC
                out["reserved_04_int"] = int(words[1])
                out["reserved_08_int"] = int(words[2])
                out["reserved_0c_int"] = int(words[3])
                out["touch_down_bool"] = bool(words[4])
                out["touch_raw_x_int"] = int(words[5])
                out["touch_raw_y_int"] = int(words[6])
                out["sadc_conversion_events_remaining_int"] = int(words[12])
                out["sadc_control_int"] = int(words[13])
                out["sadc_last_read_offset_int"] = int(words[14])
                out["sadc_last_read_value_int"] = int(words[15])
                out["sadc_last_write_offset_int"] = int(words[16])
                out["sadc_last_write_value_int"] = int(words[17])
                out["gpio_last_read_offset_int"] = int(words[18])
                out["gpio_last_read_value_int"] = int(words[19])
                out["gpio_last_flag_offset_int"] = int(words[20])
                out["gpio_last_flag_value_int"] = int(words[21])
                out["intc_mask_int"] = int(words[22])
                out["tcu_enabled_mask_int"] = int(words[23])
                out["tcu_pending_mask_int"] = int(words[24])
                out["tcu_compare0_int"] = int(words[25])
                out["tcu_compare1_int"] = int(words[26])
                out["tcu_period0_ms_int"] = int(words[27])
                out["tcu_period1_ms_int"] = int(words[28])
                out["intc_ack_count_int"] = int(words[42])
                out["intc_ack_tcu_count_int"] = int(words[43])
                out["tcu_irq_raise_count_int"] = int(words[48])
                out["reserved_c4_int"] = int(words[49])
                out["tcu_irq_mask_int"] = int(words[77])
                out["tcu_compare4_int"] = int(words[78])
                out["tcu_period4_ms_int"] = int(words[79])
                out["msc_read_pending_bool"] = bool(words[50])
                out["msc_write_pending_bool"] = bool(words[51])
                out["msc_data_ready_bool"] = bool(words[52])
                out["msc_read_lba_int"] = int(words[53])
                out["msc_write_lba_int"] = int(words[54])
                out["msc_dma_complete_count_int"] = int(words[55])
                out["msc_last_cmd_int"] = int(words[56])
                out["msc_last_arg_int"] = int(words[57])
                out["msc_last_dma_phys_int"] = int(words[58])
                out["msc_last_dma_words_int"] = int(words[59])
                out["nand_ready_raise_count_int"] = int(words[60])
                out["nand_page_read_count_int"] = int(words[61])
                out["nand_program_count_int"] = int(words[62])
                out["nand_erase_count_int"] = int(words[63])
                out["nand_last_cmd_int"] = int(words[64])
                out["nand_last_page_int"] = int(words[65])
                out["nand_last_column_int"] = int(words[66])
                out["nand_last_block_int"] = int(words[67])
                out["nand_busy_reads_int"] = int(words[68])
                out["emc_nfints_int"] = int(words[69])
                out["nand_addr_count_int"] = int(words[70])
                out["gpio_flag_200_int"] = int(words[71])
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_runtime_table_snapshot(self) -> dict[str, object]:
        """Read small runtime tables used by the current C200 wait path."""

        targets = {
            "handler_table_80370fe0": (0x80370FE0, 0x80),
            "touch_globals_80370fc0": (0x80370FC0, 0x30),
            "scheduler_80473f08": (0x80473F08, 0x60),
            "busy_node_80953ee8": (0x80953EE8, 0x80),
        }
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            if self.gdb_sock is None:
                return {"error": "QEMU GDB stub is not connected"}
            try:
                self._pause_for_gdb_locked()
                out: dict[str, object] = {}
                for name, (addr, size) in targets.items():
                    data = self._read_virtual_memory_paused_locked(addr, size)
                    words = [
                        struct.unpack_from("<I", data, offset)[0]
                        for offset in range(0, len(data) & ~3, 4)
                    ]
                    out[name] = {
                        "addr": f"0x{addr:08x}",
                        "size": size,
                        "words": [f"0x{word:08x}" for word in words],
                    }
                task_table = self._read_virtual_memory_paused_locked(0x806C5D10, 0x100)
                tasks: dict[str, object] = {}
                for index in range(64):
                    node = struct.unpack_from("<I", task_table, index * 4)[0]
                    if node == 0:
                        continue
                    node_data = self._read_virtual_memory_paused_locked(node, 0x80)
                    node_words = [
                        struct.unpack_from("<I", node_data, offset)[0]
                        for offset in range(0, 0x80, 4)
                    ]
                    tasks[str(index)] = {
                        "node": f"0x{node:08x}",
                        "words": [f"0x{word:08x}" for word in node_words],
                    }
                out["task_nodes"] = tasks
                related_addrs = [
                    0x80473F60,
                    0x80473F78,
                    0x80477CE0,
                    0x806C49C0,
                    0x806C4AA0,
                    0x806C4BB0,
                    0x806C4DB0,
                    0x806C4DCC,
                    0x806C4DE8,
                    0x806C4E04,
                    0x80729CCC,
                    0x80729D78,
                    0x807F6EEC,
                    0x8078DC24,
                    0x806C5160,
                    0x806C5298,
                    0x806C4CDC,
                    0x806C504C,
                ]
                related: dict[str, object] = {}
                for addr in related_addrs:
                    data = self._read_virtual_memory_paused_locked(addr, 0x80)
                    words = [
                        struct.unpack_from("<I", data, offset)[0]
                        for offset in range(0, 0x80, 4)
                    ]
                    related[f"0x{addr:08x}"] = [
                        f"0x{word:08x}" for word in words
                    ]
                out["related_task_memory"] = related
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_surface_trace_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 C surface helper diagnostic counters."""

        labels = (
            "gui_putpixel",
            "gui_palette",
            "setpixel",
            "hline",
            "color_span",
            "read_span",
            "block_write",
            "block_read",
            "transparent_blit",
            "fullscreen_fill",
            "boot_frame_copy",
            "logo_strip_blit",
            "portrait_blit",
            "row_copy",
            "raster_copy",
            "halfword_copy",
        )
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            if self.gdb_sock is None:
                return {"error": "QEMU GDB stub is not connected"}
            try:
                self._pause_for_gdb_locked()
                data = self._read_virtual_memory_paused_locked(DEFAULT_QEMU_SURFACE_TRACE, 0x1A0)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, 0x1A0, 4)
                ]
                counters = {
                    label: int(words[4 + index])
                    for index, label in enumerate(labels)
                }
                out: dict[str, object] = {
                    "magic": f"0x{words[0]:08x}",
                    "available": words[0] == QEMU_SURFACE_TRACE_MAGIC,
                    "total": int(words[1]),
                    "last_pc": f"0x{words[2]:08x}",
                    "counters": counters,
                    "last_fullscreen_fill": {
                        "dst": f"0x{words[0x80 // 4]:08x}",
                        "count": int(words[0x84 // 4]),
                        "color": f"0x{words[0x88 // 4] & 0xffff:04x}",
                    },
                    "last_boot_frame_copy": {
                        "src": f"0x{words[0x90 // 4]:08x}",
                        "dest_ptr": f"0x{words[0x94 // 4]:08x}",
                        "row_base": f"0x{words[0x98 // 4]:08x}",
                        "row": int(words[0x9c // 4]),
                        "col": int(words[0xa0 // 4]),
                        "pc": f"0x{words[0xa4 // 4]:08x}",
                    },
                    "last_portrait_blit": {
                        "fb": f"0x{words[0xa8 // 4]:08x}",
                        "src": f"0x{words[0xac // 4]:08x}",
                        "reverse": bool(words[0xb0 // 4]),
                        "pc": f"0x{words[0xb4 // 4]:08x}",
                    },
                    "last_block_write": {
                        "surface": f"0x{words[0xb8 // 4]:08x}",
                        "x": int(words[0xbc // 4]),
                        "y": int(words[0xc0 // 4]),
                        "width": int(words[0xc4 // 4]),
                        "height": int(words[0xc8 // 4]),
                        "buffer": f"0x{words[0xcc // 4]:08x}",
                        "stride": int(words[0xd0 // 4]),
                        "pitch": int(words[0xd4 // 4]),
                        "surface_buffer": f"0x{words[0xd8 // 4]:08x}",
                    },
                    "last_block_read": {
                        "surface": f"0x{words[0xe0 // 4]:08x}",
                        "x": int(words[0xe4 // 4]),
                        "y": int(words[0xe8 // 4]),
                        "width": int(words[0xec // 4]),
                        "height": int(words[0xf0 // 4]),
                        "buffer": f"0x{words[0xf4 // 4]:08x}",
                        "stride": int(words[0xf8 // 4]),
                        "pitch": int(words[0xfc // 4]),
                        "surface_buffer": f"0x{words[0x100 // 4]:08x}",
                    },
                    "last_transparent_blit": {
                        "surface": f"0x{words[0x108 // 4]:08x}",
                        "x": int(words[0x10c // 4]),
                        "y": int(words[0x110 // 4]),
                        "width": int(words[0x114 // 4]),
                        "height": int(words[0x118 // 4]),
                        "src": f"0x{words[0x11c // 4]:08x}",
                        "stride": int(words[0x120 // 4]),
                        "transparent": f"0x{words[0x124 // 4] & 0xffff:04x}",
                        "surface_buffer": f"0x{words[0x128 // 4]:08x}",
                    },
                    "last_glyph_mask": {
                        "glyph": f"0x{words[0x150 // 4]:08x}",
                        "dest": f"0x{words[0x154 // 4]:08x}",
                        "bit_index": int(words[0x158 // 4]),
                        "limit": int(words[0x15c // 4]),
                        "color": f"0x{words[0x160 // 4] & 0xffff:04x}",
                        "written": int(words[0x164 // 4]),
                    },
                    "last_gui_event_poller": {
                        "count": int(words[0x170 // 4]),
                        "scratch": f"0x{words[0x174 // 4]:08x}",
                        "buffer": f"0x{words[0x178 // 4]:08x}",
                        "capacity": int(words[0x17c // 4]),
                        "read_index": int(words[0x180 // 4]),
                        "write_index": int(words[0x184 // 4]),
                        "record": f"0x{words[0x188 // 4]:08x}",
                        "record_object": f"0x{words[0x18c // 4]:08x}",
                        "record_event": f"0x{words[0x190 // 4]:08x}",
                        "record_xy": f"0x{words[0x194 // 4]:08x}",
                        "return": f"0x{words[0x198 // 4]:08x}",
                    },
                }
                self.gdb_read_count += 1
                self.last_gdb_error = None
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_storage_trace_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 C storage diagnostic ring."""

        trace_va = DEFAULT_QEMU_DIAG_BASE + 0x2000
        magic = 0x53544B42
        slots = 4096
        header_words = 4
        record_words = 4
        total_words = header_words + slots * record_words

        def decode_kind(logical: int) -> str:
            if logical & 0x80000000:
                if logical & 0x04000000:
                    return "nand-read-detail"
                return "nand-read"
            if logical & 0x40000000:
                return "logical-write"
            if logical & 0x20000000:
                return "nand-program"
            if logical & 0x10000000:
                return "nand-erase"
            if logical & 0x08000000:
                return "dmac-transfer"
            return "logical-read"

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                data = self._read_guest_ram_snapshot_locked(trace_va, total_words * 4)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, total_words * 4, 4)
                ]
                seq = words[1]
                current_slot = words[2]
                reported_slots = words[3]
                records: list[dict[str, object]] = []
                for slot in range(min(slots, reported_slots or slots)):
                    base = header_words + slot * record_words
                    rec_seq = words[base]
                    if rec_seq == 0:
                        continue
                    logical = words[base + 1]
                    aux = words[base + 2]
                    first_word = words[base + 3]
                    kind = decode_kind(logical)
                    row: dict[str, object] = {
                        "slot": slot,
                        "seq": int(rec_seq),
                        "kind": kind,
                        "logical": f"0x{logical:08x}",
                        "logical_value": int(logical & 0x03FFFFFF if kind == "nand-read-detail" else logical & 0x07FFFFFF),
                        "aux": f"0x{aux:08x}",
                        "aux_value": int(aux),
                        "aux_name": "column" if kind in ("nand-read", "nand-read-detail") else "absolute_or_size",
                        "first_word": f"0x{first_word:08x}",
                    }
                    if kind == "nand-read":
                        source_value = (aux >> 24) & 0xFF
                        sources: list[str] = []
                        if source_value & 1:
                            sources.append("runtime")
                        if source_value & 2:
                            sources.append("initial")
                        if source_value & 4:
                            sources.append("ftl")
                        if source_value & 8:
                            sources.append("request-blank")
                        if source_value & 16:
                            sources.append("final-blank")
                        row.update(
                            {
                                "final_page": f"0x{aux & 0x00FFFFFF:06x}",
                                "final_page_value": int(aux & 0x00FFFFFF),
                                "source": sources,
                                "source_value": int(source_value),
                                "aux_name": "source_and_final_page",
                            }
                        )
                    elif kind == "nand-read-detail":
                        row.update({"pc": f"0x{first_word:08x}"})
                    records.append(
                        row
                    )
                records.sort(key=lambda row: int(row["seq"]))
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return {
                    "magic": f"0x{words[0]:08x}",
                    "available": words[0] == magic,
                    "seq": int(seq),
                    "current_slot": int(current_slot),
                    "slots": int(reported_slots or slots),
                    "records": records[-int(reported_slots or slots):],
                }
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_nand_read_trace_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 C NAND page-read diagnostic ring."""

        trace_va = 0x807F6800
        magic = 0x4E524B42
        slots = 64
        header_words = 4
        record_words = 11
        total_words = header_words + slots * record_words

        def decode_source(value: int) -> list[str]:
            names: list[str] = []
            if value & 1:
                names.append("runtime")
            if value & 2:
                names.append("initial")
            if value & 4:
                names.append("ftl")
            if value & 8:
                names.append("request-blank")
            if value & 16:
                names.append("final-blank")
            return names

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                data = self._read_guest_ram_snapshot_locked(trace_va, total_words * 4)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, total_words * 4, 4)
                ]
                seq = words[1]
                current_slot = words[2]
                reported_slots = words[3]
                records: list[dict[str, object]] = []
                for slot in range(min(slots, reported_slots or slots)):
                    base = header_words + slot * record_words
                    rec_seq = words[base]
                    if rec_seq == 0:
                        continue
                    source_value = words[base + 5]
                    records.append(
                        {
                            "slot": slot,
                            "seq": int(rec_seq),
                            "requested_page": f"0x{words[base + 1]:08x}",
                            "requested_page_value": int(words[base + 1]),
                            "final_page": f"0x{words[base + 2]:08x}",
                            "final_page_value": int(words[base + 2]),
                            "column": f"0x{words[base + 3]:08x}",
                            "column_value": int(words[base + 3]),
                            "first_word": f"0x{words[base + 4]:08x}",
                            "source": decode_source(source_value),
                            "source_value": int(source_value),
                            "copy_len": int(words[base + 6]),
                            "spare_seq": f"0x{words[base + 7]:08x}",
                            "spare_seq_value": int(words[base + 7]),
                            "spare_logical": f"0x{words[base + 8]:08x}",
                            "spare_logical_value": int(words[base + 8]),
                            "addr_cycles": int(words[base + 9]),
                            "pc": f"0x{words[base + 10]:08x}",
                        }
                    )
                records.sort(key=lambda row: int(row["seq"]))
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return {
                    "magic": f"0x{words[0]:08x}",
                    "available": words[0] == magic,
                    "seq": int(seq),
                    "current_slot": int(current_slot),
                    "slots": int(reported_slots or slots),
                    "records": records[-int(reported_slots or slots):],
                }
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_msc_trace_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 C MSC/DMA diagnostic ring."""

        trace_va = DEFAULT_QEMU_DIAG_BASE + 0x1000
        magic = 0x4D534B42
        slots = 113
        header_words = 4
        record_words = 9
        total_words = header_words + slots * record_words
        event_names = {1: "read", 2: "write", 3: "cmd"}

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                data = self._read_guest_ram_snapshot_locked(trace_va, total_words * 4)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, total_words * 4, 4)
                ]
                seq = words[1]
                current_slot = words[2]
                reported_slots = words[3]
                records: list[dict[str, object]] = []
                for slot in range(min(slots, reported_slots or slots)):
                    base = header_words + slot * record_words
                    rec_seq = words[base]
                    if rec_seq == 0:
                        continue
                    event = words[base + 1]
                    records.append(
                        {
                            "slot": slot,
                            "seq": int(rec_seq),
                            "event": event_names.get(event, f"event-{event}"),
                            "event_value": int(event),
                            "lba": f"0x{words[base + 2]:08x}",
                            "lba_value": int(words[base + 2]),
                            "dma_phys": f"0x{words[base + 3]:08x}",
                            "bytes": int(words[base + 4]),
                            "cmd": f"0x{words[base + 5]:08x}",
                            "arg": f"0x{words[base + 6]:08x}",
                            "first_word": f"0x{words[base + 7]:08x}",
                            "pc": f"0x{words[base + 8]:08x}",
                        }
                    )
                records.sort(key=lambda row: int(row["seq"]))
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return {
                    "magic": f"0x{words[0]:08x}",
                    "available": words[0] == magic,
                    "seq": int(seq),
                    "current_slot": int(current_slot),
                    "slots": int(reported_slots or slots),
                    "records": records[-int(reported_slots or slots):],
                }
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_fs_probe_trace_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 read-only filesystem/object diagnostic ring."""

        trace_va = DEFAULT_QEMU_DIAG_BASE + 0x20000
        magic = 0x46534B42
        slots = 96
        header_words = 4
        record_words = 96
        total_words = header_words + slots * record_words
        labels = (
            "seq",
            "pc",
            "v0",
            "a0",
            "a1",
            "a2",
            "a3",
            "sp",
            "ra",
            "obj",
            "obj_18",
            "obj_20",
            "obj_30",
            "obj_34",
            "obj_48_h",
            "obj_4a_h",
            "obj_24",
            "obj_38",
            "obj_44",
            "obj_50",
            "s0",
            "s1",
            "s2",
            "s3",
            "s4",
            "s5",
            "s6",
            "s7",
            "fp",
            "dirent",
            "dirent_00",
            "dirent_04",
            "dirent_08",
            "dirent_0c",
            "dirent_10",
            "dirent_14",
            "dirent_18",
            "dirent_1c",
            "cache_desc",
            "cache_desc_00",
            "cache_desc_04",
            "cache_desc_08",
            "cache_flags",
            "cache_data",
            "cache_sector",
            "cache_page_index",
            "cache_sector_in_page",
            "cache_flag_va",
            "cache_flag",
            "cache_data_va",
            "cache_data_00",
            "cache_data_04",
            "cache_data_08",
            "cache_data_0c",
            "slot0_block",
            "slot0_state",
            "slot0_use",
            "slot0_data",
            "slot1_block",
            "slot1_state",
            "slot1_use",
            "slot1_data",
            "slot2_block",
            "slot2_state",
            "slot2_use",
            "slot2_data",
            "slot3_block",
            "slot3_state",
            "slot3_use",
            "slot3_data",
            "slot4_block",
            "slot4_state",
            "slot4_use",
            "slot4_data",
            "slot5_block",
            "slot5_state",
            "slot5_use",
            "slot5_data",
            "ftl_free_map",
            "ftl_state_map",
            "ftl_block_count",
            "ftl_pages_per_block",
            "ftl_block_bytes",
            "ftl_page_bytes",
            "ftl_next_free",
            "free_000",
            "free_3cc",
            "free_a2b",
            "free_a30",
            "free_b20",
            "ftl_b48",
            "ftl_b54",
            "ftl_b50",
            "ftl_b44",
        )

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                data = self._read_guest_ram_snapshot_locked(trace_va, total_words * 4)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, total_words * 4, 4)
                ]
                seq = words[1]
                current_slot = words[2]
                reported_slots = words[3]
                records: list[dict[str, object]] = []
                for slot in range(min(slots, reported_slots or slots)):
                    base = header_words + slot * record_words
                    rec_seq = words[base]
                    if rec_seq == 0:
                        continue
                    row = {"slot": slot}
                    for index, label in enumerate(labels):
                        value = words[base + index]
                        row[label] = int(value) if label == "seq" else f"0x{value:08x}"
                    records.append(row)
                records.sort(key=lambda row: int(row["seq"]))
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return {
                    "magic": f"0x{words[0]:08x}",
                    "available": words[0] == magic,
                    "seq": int(seq),
                    "current_slot": int(current_slot),
                    "slots": int(reported_slots or slots),
                    "records": records[-int(reported_slots or slots):],
                }
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def guest_progress_trace_snapshot(self) -> dict[str, object]:
        """Read the bbk9588 C progress diagnostic ring."""

        trace_va = DEFAULT_QEMU_DIAG_BASE + 0x0500
        magic = 0x50544B42
        slots = 8
        header_words = 4
        record_words = 12
        total_words = header_words + slots * record_words

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            try:
                if self.gdb_sock is not None:
                    self._pause_for_gdb_locked()
                data = self._read_guest_ram_snapshot_locked(trace_va, total_words * 4)
                words = [
                    struct.unpack_from("<I", data, offset)[0]
                    for offset in range(0, total_words * 4, 4)
                ]
                seq = words[1]
                current_slot = words[2]
                reported_slots = words[3]
                records: list[dict[str, object]] = []
                for slot in range(min(slots, reported_slots or slots)):
                    base = header_words + slot * record_words
                    rec_seq = words[base]
                    if rec_seq == 0:
                        continue
                    records.append(
                        {
                            "slot": slot,
                            "seq": int(rec_seq),
                            "reason": f"0x{words[base + 1]:08x}",
                            "pc": f"0x{words[base + 2]:08x}",
                            "intc_pending": f"0x{words[base + 3]:08x}",
                            "intc_mask": f"0x{words[base + 4]:08x}",
                            "tcu_pending": f"0x{words[base + 5]:08x}",
                            "resource_flags_804bf440": f"0x{words[base + 6]:08x}",
                            "resource_refresh_804bf444": f"0x{words[base + 7]:08x}",
                            "scheduler_80473f08": f"0x{words[base + 8]:08x}",
                            "scheduler_group_80473f38": f"0x{words[base + 9]:08x}",
                            "cp0_cause": f"0x{words[base + 10]:08x}",
                            "cp0_status": f"0x{words[base + 11]:08x}",
                        }
                    )
                records.sort(key=lambda row: int(row["seq"]))
                if self.gdb_sock is not None:
                    self.gdb_read_count += 1
                    self.last_gdb_error = None
                return {
                    "magic": f"0x{words[0]:08x}",
                    "available": words[0] == magic,
                    "seq": int(seq),
                    "current_slot": int(current_slot),
                    "slots": int(reported_slots or slots),
                    "records": records[-slots:],
                }
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                if self.gdb_sock is not None:
                    try:
                        self._resume_after_gdb_locked()
                    except Exception as exc:
                        self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _rgb565_frame_stats(data: bytes, *, width: int = 240, height: int = 320) -> dict[str, object]:
        pixels = min(len(data) // 2, width * height)
        counts: dict[int, int] = {}
        nonzero = 0
        white = 0
        black = 0
        for index in range(pixels):
            value = data[index * 2] | (data[index * 2 + 1] << 8)
            counts[value] = counts.get(value, 0) + 1
            if value:
                nonzero += 1
            if value == 0xffff:
                white += 1
            elif value == 0:
                black += 1
        top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
        return {
            "pixels": pixels,
            "nonzero": nonzero,
            "white_ffff": white,
            "black_0000": black,
            "unique": len(counts),
            "top_values": [
                {"value": f"0x{value:04x}", "count": count}
                for value, count in top
            ],
        }

    def _read_virtual_memory_chunked_paused_locked(self, addr: int, size: int, chunk: int = 0x1000) -> bytes:
        out = bytearray()
        offset = 0
        while offset < size:
            count = min(chunk, size - offset)
            out += self._read_virtual_memory_paused_locked(addr + offset, count)
            offset += count
        return bytes(out)

    def read_guest_memory(self, addr: int, size: int) -> bytes:
        """Read a guest virtual RAM span through the active GDB connection."""

        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("QEMU process is not running")
            if self.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            if not self._is_guest_ram_va(addr, size):
                raise ValueError(f"guest span is outside RAM: 0x{addr:08x}+0x{size:x}")
            try:
                self._pause_for_gdb_locked()
                data = self._read_virtual_memory_chunked_paused_locked(addr, size)
                self.gdb_read_count += 1
                self.last_gdb_error = None
                return data
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def watch_guest_write_once(
        self,
        addr: int,
        size: int = 4,
        timeout: float = 10.0,
        trigger_touch: tuple[int, int] | None = None,
        trigger_hold_seconds: float = 0.0,
        ignore_pcs: tuple[int, ...] = (),
        max_hits: int = 1,
    ) -> dict[str, object]:
        """Stop once when the guest writes a virtual RAM span."""

        size = max(1, min(int(size), 8))
        timeout = max(0.1, min(float(timeout), 120.0))
        row: dict[str, object] = {
            "addr": f"0x{addr & 0xFFFFFFFF:08x}",
            "size": size,
            "timeout": timeout,
            "hit": False,
            "ignored_hits": [],
            "events": [],
        }
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                row["error"] = "QEMU process is not running"
                return row
            if self.gdb_sock is None:
                row["error"] = "QEMU GDB stub is not connected"
                return row
            if not self._is_guest_ram_va(addr, size):
                row["error"] = f"guest span is outside RAM: 0x{addr:08x}+0x{size:x}"
                return row
            inserted = False
            try:
                self._pause_for_gdb_locked()
                reply = _gdb_packet(self.gdb_sock, f"Z2,{addr & 0xFFFFFFFF:x},{size:x}")
                if reply != "OK":
                    row["error"] = f"GDB could not insert write watchpoint: {reply!r}"
                    return row
                inserted = True
                if trigger_touch is not None:
                    x, y = trigger_touch
                    raw_x, raw_y = self._touch_panel_to_adc(x, y)
                    hold_seconds = max(0.0, min(float(trigger_hold_seconds), 2.0))
                    row["trigger_touch"] = {
                        "x": x,
                        "y": y,
                        "raw_x": f"0x{raw_x:04x}",
                        "raw_y": f"0x{raw_y:04x}",
                        "hold_seconds": hold_seconds,
                    }
                    if self.bbk_input_sock is None:
                        row["trigger_touch"]["sent"] = False
                        row["trigger_touch"]["error"] = "QEMU input chardev is not connected"
                    else:
                        down_sent = self._send_bbk_input_locked(
                            f"T {x} {y} {raw_x} {raw_y} 1"
                        )
                        if hold_seconds > 0:
                            _gdb_continue(self.gdb_sock)
                            time.sleep(hold_seconds)
                            try:
                                row["trigger_touch"]["hold_stop"] = _gdb_interrupt(self.gdb_sock)
                            except Exception as exc:
                                row["trigger_touch"]["hold_stop_error"] = f"{type(exc).__name__}: {exc}"
                        up_sent = self._send_bbk_input_locked(
                            f"T {x} {y} {raw_x} {raw_y} 0"
                        )
                        row["trigger_touch"]["sent"] = bool(down_sent and up_sent)
                pc = None
                stop_reply = ""
                limit = max(1, min(int(max_hits), 2048))
                for _ in range(limit):
                    stop_reply = _gdb_continue_wait(self.gdb_sock, timeout=timeout)
                    pc = self._read_pc_paused_locked()
                    if pc is not None and (pc & 0xFFFFFFFF) in ignore_pcs:
                        row["ignored_hits"].append(
                            {
                                "stop_reply": stop_reply,
                                "pc": f"0x{pc & 0xFFFFFFFF:08x}",
                            }
                        )
                        _gdb_packet(self.gdb_sock, f"z2,{addr & 0xFFFFFFFF:x},{size:x}")
                        self._step_paused_locked()
                        _gdb_packet(self.gdb_sock, f"Z2,{addr & 0xFFFFFFFF:x},{size:x}")
                        continue
                    event: dict[str, object] = {
                        "stop_reply": stop_reply,
                    }
                    if pc is not None:
                        event["pc"] = f"0x{pc & 0xFFFFFFFF:08x}"
                    for regno, name in (
                        (31, "ra"),
                        (29, "sp"),
                        (4, "a0"),
                        (5, "a1"),
                        (6, "a2"),
                        (7, "a3"),
                        (16, "s0"),
                        (17, "s1"),
                        (18, "s2"),
                        (19, "s3"),
                        (20, "s4"),
                        (21, "s5"),
                        (22, "s6"),
                        (23, "s7"),
                        (30, "s8"),
                    ):
                        event[name] = f"0x{self._read_register_paused_locked(regno) & 0xFFFFFFFF:08x}"
                    try:
                        event["watched_hex"] = self._read_virtual_memory_paused_locked(addr, size).hex()
                    except Exception as exc:
                        event["watched_error"] = f"{type(exc).__name__}: {exc}"
                    row["events"].append(event)
                    row["hit"] = True
                    if len(row["events"]) >= limit:
                        break
                    _gdb_packet(self.gdb_sock, f"z2,{addr & 0xFFFFFFFF:x},{size:x}")
                    self._step_paused_locked()
                    _gdb_packet(self.gdb_sock, f"Z2,{addr & 0xFFFFFFFF:x},{size:x}")
                if row["events"]:
                    last_event = row["events"][-1]
                    row["pc"] = last_event.get("pc")
                    row["ra"] = last_event.get("ra")
                    row["sp"] = last_event.get("sp")
                    row["a0"] = last_event.get("a0")
                    row["a1"] = last_event.get("a1")
                    row["a2"] = last_event.get("a2")
                    row["a3"] = last_event.get("a3")
                    for name in ("s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
                        row[name] = last_event.get(name)
                    self.gdb_register_read_count += 15 * len(row["events"])
                    self.last_gdb_error = None
                    return row
                row["stop_reply"] = stop_reply
                if pc is not None:
                    row["pc"] = f"0x{pc & 0xFFFFFFFF:08x}"
                row["ra"] = f"0x{self._read_register_paused_locked(31) & 0xFFFFFFFF:08x}"
                row["sp"] = f"0x{self._read_register_paused_locked(29) & 0xFFFFFFFF:08x}"
                row["a0"] = f"0x{self._read_register_paused_locked(4) & 0xFFFFFFFF:08x}"
                row["a1"] = f"0x{self._read_register_paused_locked(5) & 0xFFFFFFFF:08x}"
                row["a2"] = f"0x{self._read_register_paused_locked(6) & 0xFFFFFFFF:08x}"
                row["a3"] = f"0x{self._read_register_paused_locked(7) & 0xFFFFFFFF:08x}"
                sp = self._read_register_paused_locked(29) & 0xFFFFFFFF
                for regno, name in (
                    (16, "s0"),
                    (17, "s1"),
                    (18, "s2"),
                    (19, "s3"),
                    (20, "s4"),
                    (21, "s5"),
                    (22, "s6"),
                    (23, "s7"),
                    (30, "s8"),
                ):
                    row[name] = f"0x{self._read_register_paused_locked(regno) & 0xFFFFFFFF:08x}"
                if self._is_guest_ram_va(sp, 0x80):
                    row["stack_addr"] = f"0x{sp:08x}"
                    row["stack_hex"] = self._read_virtual_memory_paused_locked(sp, 0x80).hex()
                row["hit"] = True
                self.gdb_register_read_count += 17
                self.last_gdb_error = None
                return row
            except TimeoutError:
                row["timeout_waiting_for_watchpoint"] = True
                try:
                    row["timeout_interrupt_stop"] = _gdb_interrupt(self.gdb_sock)
                except Exception as exc:
                    row["timeout_interrupt_error"] = f"{type(exc).__name__}: {exc}"
                return row
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                row["error"] = self.last_gdb_error
                return row
            finally:
                if inserted and self.gdb_sock is not None:
                    try:
                        _gdb_packet(self.gdb_sock, f"z2,{addr & 0xFFFFFFFF:x},{size:x}")
                    except Exception as exc:
                        row["watch_remove_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def trace_guest_breakpoints_once(
        self,
        pcs: tuple[int, ...],
        *,
        timeout: float = 5.0,
        max_hits: int = 32,
        trigger_touch: tuple[int, int] | None = None,
        trigger_hold_seconds: float = 0.0,
        sample_rect: tuple[int, int, int, int] | None = None,
        dedupe_blits: bool = False,
    ) -> dict[str, object]:
        """Record register/stack state at guest breakpoints without changing guest memory."""

        active = {pc & 0xFFFFFFFF for pc in pcs}
        row: dict[str, object] = {
            "breakpoints": [f"0x{pc:08x}" for pc in sorted(active)],
            "timeout": timeout,
            "events": [],
        }
        seen_blits: dict[tuple[int, int, int, int, int, int], int] = {}
        seen_draws: dict[tuple[int, int, int, int, int], int] = {}
        seen_resource_copies: dict[tuple[int, int, int, int, int], int] = {}
        if not active:
            row["error"] = "no breakpoints requested"
            return row
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                row["error"] = "QEMU process is not running"
                return row
            if self.gdb_sock is None:
                row["error"] = "QEMU GDB stub is not connected"
                return row
            inserted: list[int] = []
            trigger_thread: threading.Thread | None = None
            trigger_thread_started = False
            deadline = time.time() + max(0.1, min(float(timeout), 120.0))
            try:
                self._pause_for_gdb_locked()
                for pc in sorted(active):
                    reply = _gdb_insert_breakpoint(self.gdb_sock, pc)
                    if reply != "OK":
                        row["error"] = f"failed to insert breakpoint 0x{pc:08x}: {reply!r}"
                        return row
                    inserted.append(pc)
                if trigger_touch is not None:
                    x, y = trigger_touch
                    raw_x, raw_y = self._touch_panel_to_adc(x, y)
                    hold_seconds = max(0.0, min(float(trigger_hold_seconds), 2.0))
                    row["trigger_touch"] = {
                        "x": x,
                        "y": y,
                        "raw_x": f"0x{raw_x:04x}",
                        "raw_y": f"0x{raw_y:04x}",
                        "hold_seconds": hold_seconds,
                    }
                    if self.bbk_input_sock is None:
                        row["trigger_touch"]["error"] = "QEMU input chardev is not connected"
                    trigger_sock = self.bbk_input_sock
                    if trigger_sock is not None:
                        def send_trigger_touch() -> None:
                            try:
                                time.sleep(0.05)
                                trigger_sock.sendall((f"T {x} {y} {raw_x} {raw_y} 1\n").encode("ascii"))
                                if hold_seconds > 0:
                                    time.sleep(hold_seconds)
                                trigger_sock.sendall((f"T {x} {y} {raw_x} {raw_y} 0\n").encode("ascii"))
                            except Exception as exc:
                                row["trigger_touch"]["async_error"] = f"{type(exc).__name__}: {exc}"

                        trigger_thread = threading.Thread(
                            target=send_trigger_touch,
                            name="bbk9588-trace-trigger-touch",
                            daemon=True,
                        )
                while len(row["events"]) < max(1, min(int(max_hits), 1024)) and time.time() < deadline:
                    try:
                        if trigger_thread is not None and not trigger_thread_started:
                            trigger_thread_started = True
                            row["trigger_touch"]["async_started"] = True
                            trigger_thread.start()
                        stop = _gdb_continue_wait(
                            self.gdb_sock,
                            timeout=max(0.05, min(0.25, deadline - time.time())),
                        )
                    except socket.timeout:
                        row["timeout_waiting_for_breakpoint"] = True
                        try:
                            row["timeout_interrupt_stop"] = _gdb_interrupt(self.gdb_sock)
                        except Exception as exc:
                            row["timeout_interrupt_error"] = f"{type(exc).__name__}: {exc}"
                        break
                    pc = self._read_register_paused_locked(37) & 0xFFFFFFFF
                    event: dict[str, object] = {
                        "pc": f"0x{pc:08x}",
                        "stop_reply": stop,
                        "matched": pc in active,
                    }
                    for regno, name in (
                        (2, "v0"),
                        (3, "v1"),
                        (4, "a0"),
                        (5, "a1"),
                        (6, "a2"),
                        (7, "a3"),
                        (16, "s0"),
                        (17, "s1"),
                        (18, "s2"),
                        (19, "s3"),
                        (20, "s4"),
                        (21, "s5"),
                        (22, "s6"),
                        (23, "s7"),
                        (29, "sp"),
                        (30, "s8"),
                        (31, "ra"),
                    ):
                        event[name] = f"0x{self._read_register_paused_locked(regno) & 0xFFFFFFFF:08x}"
                    def sample_guest(addr: int, size: int) -> str | None:
                        size = max(0, min(int(size), 0x400))
                        if size <= 0 or not self._is_guest_ram_va(addr, size):
                            return None
                        return self._read_virtual_memory_paused_locked(addr, size).hex()

                    def sample_guest_limited(addr: int, size: int, limit: int) -> str | None:
                        size = max(0, min(int(size), int(limit)))
                        if size <= 0 or not self._is_guest_ram_va(addr, size):
                            return None
                        return self._read_virtual_memory_paused_locked(addr, size).hex()

                    if pc in {
                        0x8002D1B8,
                        0x8002D21C,
                        0x8002D0E8,
                        0x8002D100,
                        0x8002D104,
                        0x8002D13C,
                        0x8002D144,
                        0x8002D154,
                        0x8002D15C,
                        0x8002D16C,
                        0x8002D174,
                        0x8002D17C,
                        0x8002D720,
                        0x8002D7E8,
                        0x8002D88C,
                        0x8002D8D8,
                        0x8002D920,
                        0x8002D940,
                        0x8002D954,
                        0x80006BF8,
                        0x800E2868,
                        0x8017B4E0,
                        0x8017B718,
                        0x8017B5C8,
                        0x8017B618,
                        0x8017B638,
                        0x8017B670,
                        0x8017B6D4,
                        0x8017B700,
                        0x8017B748,
                        0x8017B9A4,
                        0x8017B854,
                        0x8017CA10,
                        0x80170EC8,
                        0x80170ED0,
                        0x80182D6C,
                        0x80182EF0,
                        0x80182F2C,
                        0x80182FB8,
                        0x80183D04,
                        0x80184860,
                    }:
                        samples: dict[str, object] = {}
                        for key in ("a0", "a1", "s0", "s2", "s4"):
                            addr = int(str(event[key]), 16)
                            data = sample_guest(addr, 0x40)
                            if data is not None:
                                samples[key] = {"addr": f"0x{addr:08x}", "hex": data}
                        if pc in {
                            0x8002D1B8,
                            0x8002D21C,
                            0x8002D0E8,
                            0x8002D100,
                            0x8002D104,
                            0x8002D13C,
                            0x8002D144,
                            0x8002D154,
                            0x8002D15C,
                            0x8002D16C,
                            0x8002D174,
                            0x8002D17C,
                            0x8002D720,
                            0x8002D7E8,
                            0x8002D88C,
                            0x8002D8D8,
                            0x8002D920,
                            0x8002D940,
                            0x8002D954,
                        }:
                            for key in ("v0", "v1", "a0", "a1", "a2", "a3", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7"):
                                addr = int(str(event[key]), 16)
                                data = sample_guest_limited(addr, 0x180, 0x180)
                                if data is not None:
                                    samples[f"{key}_icon_dispatch"] = {
                                        "addr": f"0x{addr:08x}",
                                        "hex": data,
                                    }
                                    path_addr = (addr + 0x28) & 0xFFFFFFFF
                                    path_data = sample_guest_limited(path_addr, 0x100, 0x100)
                                    if path_data is not None:
                                        samples[f"{key}_record_path"] = {
                                            "addr": f"0x{path_addr:08x}",
                                            "hex": path_data,
                                        }
                            for name, addr in (
                                ("resource_dst_807fc320", 0x807FC320),
                                ("resource_dst_807fff20", 0x807FFF20),
                                ("resource_dst_8080c320", 0x8080C320),
                            ):
                                data = sample_guest_limited(addr, 0x1800, 0x1800)
                                if data is not None:
                                    samples[name] = {
                                        "addr": f"0x{addr:08x}",
                                        "hex": data,
                                    }
                        if pc == 0x80170EC8:
                            src = int(str(event["a1"]), 16)
                            dst = int(str(event["a0"]), 16)
                            length = int(str(event["a2"]), 16)
                            for name, base in (("src", src), ("dst", dst)):
                                for off in (0x400, 0x800, 0x900, 0xA00, 0xC00, 0xF90, 0x1200, 0x1600):
                                    if off < length:
                                        data = sample_guest((base + off) & 0xFFFFFFFF, 0x80)
                                        if data is not None:
                                            samples[f"{name}_plus_0x{off:x}"] = {
                                                "addr": f"0x{(base + off) & 0xFFFFFFFF:08x}",
                                                "hex": data,
                                            }
                        if pc == 0x80170ED0:
                            dst = int(str(event["s5"]), 16)
                            length = int(str(event["s0"]), 16)
                            samples["resource_copy_result"] = {
                                "dst": f"0x{dst:08x}",
                                "length": f"0x{length:08x}",
                                "file_offset_after": event["s3"],
                                "remaining_after": event["s2"],
                            }
                            resource_key = (
                                dst,
                                length,
                                int(str(event["s3"]), 16),
                                int(str(event["s6"]), 16),
                                int(str(event["s7"]), 16),
                            )
                            resource_count = seen_resource_copies.get(resource_key, 0) + 1
                            seen_resource_copies[resource_key] = resource_count
                            if resource_count > 1:
                                event["duplicate_resource_copy"] = resource_count
                            for off in (0x0, 0x18, 0x400, 0x800, 0x1200, 0x1600):
                                if off < length:
                                    data = sample_guest((dst + off) & 0xFFFFFFFF, 0x100)
                                    if data is not None:
                                        samples[f"dst_after_plus_0x{off:x}"] = {
                                            "addr": f"0x{(dst + off) & 0xFFFFFFFF:08x}",
                                            "hex": data,
                                        }
                            if dst in {0x807FC320, 0x807FFF20} and 0 < length <= 0x4000:
                                data = sample_guest_limited(dst, length, 0x4000)
                                if data is not None:
                                    samples["dst_after_full"] = {
                                        "addr": f"0x{dst:08x}",
                                        "size": f"0x{length:08x}",
                                        "hex": data,
                                    }
                        if pc in {
                            0x8017B4E0,
                            0x8017B718,
                            0x8017B5C8,
                            0x8017B618,
                            0x8017B638,
                            0x8017B670,
                            0x8017B6D4,
                            0x8017B700,
                        }:
                            table = sample_guest_limited(0x8086D200, 0xC0, 0xC0)
                            if table is not None:
                                samples["cluster_read_cache_8086d200"] = table
                            event["cluster_read_request"] = {
                                "cluster": event["a0"] if pc == 0x8017B4E0 else event["s2"],
                                "dst_or_mode": event["a1"] if pc == 0x8017B4E0 else event["s4"],
                            }
                        if pc in {0x8017B748, 0x8017B9A4, 0x8017B854}:
                            table = sample_guest_limited(0x8086D2C0, 0xC0, 0xC0)
                            if table is not None:
                                samples["cluster_alt_cache_8086d2c0"] = table
                            event["cluster_alt_request"] = {
                                "cluster": event["a0"] if pc == 0x8017B748 else event["s2"],
                                "dst_or_mode": event["a1"] if pc == 0x8017B748 else event["s4"],
                            }
                        if pc == 0x8017CA10:
                            table = sample_guest_limited(0x8086D180, 0x80, 0x80)
                            if table is not None:
                                samples["fat16_cache_8086d180"] = table
                        desc = sample_guest(0x804BF4C0, 0x80)
                        if desc is not None:
                            samples["cache_desc_804bf4c0"] = desc
                        globals_data = sample_guest(0x804CF460, 0x100)
                        if globals_data is not None:
                            samples["storage_globals_804cf460"] = globals_data
                        index = int(str(event["s6"]), 16)
                        s2 = int(str(event["s2"]), 16)
                        indexed = sample_guest((s2 + index * 0x200) & 0xFFFFFFFF, 0x40)
                        if indexed is not None:
                            samples["s2_plus_s6_sector"] = {
                                "addr": f"0x{(s2 + index * 0x200) & 0xFFFFFFFF:08x}",
                                "hex": indexed,
                            }
                        if pc == 0x800E2868:
                            draw_arg_key = None
                            for name in ("s3", "s4", "s6"):
                                addr = int(str(event[name]), 16)
                                data = sample_guest_limited(addr, 0x80, 0x80)
                                if data is not None:
                                    samples[f"{name}_object"] = {
                                        "addr": f"0x{addr:08x}",
                                        "hex": data,
                                    }
                            sp_addr = int(str(event["sp"]), 16)
                            if self._is_guest_ram_va(sp_addr, 0x80):
                                stack_now = self._read_virtual_memory_paused_locked(sp_addr, 0x80)
                                # Breakpoint is at the first instruction of 0x800e2868, before
                                # the callee subtracts 0x60 from sp. Stack args are still at
                                # caller offsets 0x10/0x14/0x18 here.
                                for off, label in ((0x10, "draw_width"), (0x14, "draw_buffer"), (0x18, "draw_colorkey")):
                                    value = struct.unpack_from("<I", stack_now, off)[0]
                                    event[label] = f"0x{value:08x}"
                                buffer_addr = struct.unpack_from("<I", stack_now, 0x14)[0]
                                data = sample_guest_limited(buffer_addr, 0x1800, 0x1800)
                                if data is not None:
                                    samples["draw_buffer_arg"] = {
                                        "addr": f"0x{buffer_addr:08x}",
                                        "hex": data,
                                    }
                                draw_arg_key = (
                                    int(str(event["ra"]), 16),
                                    int(str(event["a1"]), 16),
                                    int(str(event["a2"]), 16),
                                    int(str(event["a3"]), 16),
                                    buffer_addr,
                                )
                            if draw_arg_key is not None:
                                count = seen_draws.get(draw_arg_key, 0) + 1
                                seen_draws[draw_arg_key] = count
                                if count > 1:
                                    event["duplicate_draw"] = count
                        if samples:
                            event["mem_samples"] = samples
                    sp = int(str(event["sp"]), 16)
                    if self._is_guest_ram_va(sp, 0x80):
                        stack = self._read_virtual_memory_paused_locked(sp, 0x80)
                        event["stack_hex"] = stack.hex()
                        if pc == 0x8012BE64:
                            surface_base = int(str(event["a0"]), 16)
                            pixel_offset = int(str(event["a1"]), 16)
                            pixel_value = int(str(event["s0"]), 16) & 0xFFFF
                            pixel_addr = (surface_base + pixel_offset) & 0xFFFFFFFF
                            event["pixel_addr"] = f"0x{pixel_addr:08x}"
                            event["pixel_value"] = f"0x{pixel_value:04x}"
                            if sample_rect is not None:
                                sx, sy, sw, sh = sample_rect
                                stride = 0x1E0
                                if surface_base in {0x80825B90, 0x80F96074} and stride:
                                    pixel_index = pixel_offset // 2
                                    px = pixel_index % (stride // 2)
                                    py = pixel_index // (stride // 2)
                                    event["pixel_x"] = px
                                    event["pixel_y"] = py
                                    if not (sx <= px < sx + sw and sy <= py < sy + sh):
                                        event["outside_sample_rect"] = True
                                else:
                                    event["outside_sample_rect"] = True
                        def sample_rows(
                            *,
                            src_base: int,
                            src_stride: int,
                            dst_x: int,
                            dst_y: int,
                            dst_w: int,
                            dst_h: int,
                        ) -> dict[str, object] | None:
                            if sample_rect is None:
                                return None
                            sx, sy, sw, sh = sample_rect
                            ix0 = max(sx, dst_x)
                            iy0 = max(sy, dst_y)
                            ix1 = min(sx + sw, dst_x + dst_w)
                            iy1 = min(sy + sh, dst_y + dst_h)
                            if ix0 >= ix1 or iy0 >= iy1:
                                return None
                            rows: list[str] = []
                            for yy in range(iy0, iy1):
                                src = (src_base + (yy - dst_y) * src_stride + (ix0 - dst_x) * 2) & 0xFFFFFFFF
                                size = (ix1 - ix0) * 2
                                if self._is_guest_ram_va(src, size):
                                    rows.append(self._read_virtual_memory_paused_locked(src, size).hex())
                            return {
                                "x": ix0,
                                "y": iy0,
                                "w": ix1 - ix0,
                                "h": iy1 - iy0,
                                "rows_hex": rows,
                            }

                        if pc == 0x8012C1BC:
                            block_height = struct.unpack_from("<I", stack, 0x10)[0]
                            block_buffer = struct.unpack_from("<I", stack, 0x14)[0]
                            block_stride = struct.unpack_from("<I", stack, 0x18)[0]
                            event["block_height"] = f"0x{block_height:08x}"
                            event["block_buffer"] = f"0x{block_buffer:08x}"
                            event["block_stride"] = f"0x{block_stride:08x}"
                            sampled = sample_rows(
                                src_base=block_buffer,
                                src_stride=block_stride,
                                dst_x=int(str(event["a1"]), 16),
                                dst_y=int(str(event["a2"]), 16),
                                dst_w=int(str(event["a3"]), 16),
                                dst_h=block_height,
                            )
                            if sampled is not None:
                                event["sample_rect"] = sampled
                        if pc == 0x800A9784:
                            draw_height = struct.unpack_from("<I", stack, 0x10)[0]
                            draw_buffer = struct.unpack_from("<I", stack, 0x14)[0]
                            draw_transparent = struct.unpack_from("<I", stack, 0x18)[0]
                            draw_stride = int(str(event["a3"]), 16) * 2
                            event["draw_height"] = f"0x{draw_height:08x}"
                            event["draw_buffer"] = f"0x{draw_buffer:08x}"
                            event["draw_stride"] = f"0x{draw_stride:08x}"
                            event["draw_transparent"] = f"0x{draw_transparent:08x}"
                            if dedupe_blits:
                                draw_key = (
                                    int(str(event["a1"]), 16),
                                    int(str(event["a2"]), 16),
                                    int(str(event["a3"]), 16),
                                    draw_height,
                                    draw_buffer,
                                )
                                count = seen_draws.get(draw_key, 0) + 1
                                seen_draws[draw_key] = count
                                if count > 1:
                                    event["duplicate_draw"] = count
                            sampled = sample_rows(
                                src_base=draw_buffer,
                                src_stride=draw_stride,
                                dst_x=int(str(event["a1"]), 16),
                                dst_y=int(str(event["a2"]), 16),
                                dst_w=int(str(event["a3"]), 16),
                                dst_h=draw_height,
                            )
                            if sampled is not None:
                                event["sample_rect"] = sampled
                        if pc == 0x8012C3D0:
                            read_height = struct.unpack_from("<I", stack, 0x10)[0]
                            read_buffer = struct.unpack_from("<I", stack, 0x14)[0]
                            read_stride = struct.unpack_from("<I", stack, 0x18)[0]
                            event["read_height"] = f"0x{read_height:08x}"
                            event["read_buffer"] = f"0x{read_buffer:08x}"
                            event["read_stride"] = f"0x{read_stride:08x}"
                            sampled = sample_rows(
                                src_base=read_buffer,
                                src_stride=read_stride,
                                dst_x=int(str(event["a1"]), 16),
                                dst_y=int(str(event["a2"]), 16),
                                dst_w=int(str(event["a3"]), 16),
                                dst_h=read_height,
                            )
                            if sampled is not None:
                                event["sample_rect"] = sampled
                        if pc == 0x8012C46C:
                            blit_height = struct.unpack_from("<I", stack, 0x10)[0]
                            blit_buffer = struct.unpack_from("<I", stack, 0x14)[0]
                            blit_stride = struct.unpack_from("<I", stack, 0x18)[0]
                            blit_transparent = struct.unpack_from("<I", stack, 0x1c)[0]
                            event["blit_height"] = f"0x{blit_height:08x}"
                            event["blit_buffer"] = f"0x{blit_buffer:08x}"
                            event["blit_stride"] = f"0x{blit_stride:08x}"
                            event["blit_transparent"] = f"0x{blit_transparent:08x}"
                            if dedupe_blits:
                                blit_key = (
                                    int(str(event["a1"]), 16),
                                    int(str(event["a2"]), 16),
                                    int(str(event["a3"]), 16),
                                    blit_height,
                                    blit_buffer,
                                    blit_stride,
                                )
                                count = seen_blits.get(blit_key, 0) + 1
                                seen_blits[blit_key] = count
                                if count > 1:
                                    event["duplicate_blit"] = count
                            sampled = sample_rows(
                                src_base=blit_buffer,
                                src_stride=blit_stride,
                                dst_x=int(str(event["a1"]), 16),
                                dst_y=int(str(event["a2"]), 16),
                                dst_w=int(str(event["a3"]), 16),
                                dst_h=blit_height,
                            )
                            if sampled is not None:
                                event["sample_rect"] = sampled
                            blit_full_size = int(str(event["a3"]), 16) * blit_height * 2
                            if (
                                blit_buffer in {0x807FC338, 0x80BFC7EC, 0x80C3240C}
                                and 0 < blit_full_size <= 0x4000
                                and self._is_guest_ram_va(blit_buffer, blit_full_size)
                            ):
                                event["blit_source_full"] = {
                                    "addr": f"0x{blit_buffer:08x}",
                                    "w": event["a3"],
                                    "h": f"0x{blit_height:08x}",
                                    "stride": f"0x{blit_stride:08x}",
                                    "hex": self._read_virtual_memory_paused_locked(blit_buffer, blit_full_size).hex(),
                                }
                            surface = int(str(event["a0"]), 16)
                            if self._is_guest_ram_va(surface, 0x48):
                                try:
                                    surface_data = self._read_virtual_memory_paused_locked(surface, 0x48)
                                    dst_pitch = struct.unpack_from("<I", surface_data, 0x18)[0]
                                    dst_buffer = struct.unpack_from("<I", surface_data, 0x44)[0]
                                    event["dest_buffer"] = f"0x{dst_buffer:08x}"
                                    event["dest_stride"] = f"0x{dst_pitch:08x}"
                                    dst_sampled = sample_rows(
                                        src_base=dst_buffer,
                                        src_stride=dst_pitch,
                                        dst_x=int(str(event["a1"]), 16),
                                        dst_y=int(str(event["a2"]), 16),
                                        dst_w=int(str(event["a3"]), 16),
                                        dst_h=blit_height,
                                    )
                                    if dst_sampled is not None:
                                        event["dest_sample_rect"] = dst_sampled
                                except Exception as exc:
                                    event["dest_sample_error"] = f"{type(exc).__name__}: {exc}"
                        if pc == 0x8012BF28:
                            event["row_buffer"] = f"0x{struct.unpack_from('<I', stack, 0x10)[0]:08x}"
                    if event.get("outside_sample_rect"):
                        row["outside_sample_rect_skipped"] = int(row.get("outside_sample_rect_skipped", 0)) + 1
                    elif event.get("duplicate_resource_copy"):
                        row["duplicate_resource_copies_skipped"] = int(
                            row.get("duplicate_resource_copies_skipped", 0)
                        ) + 1
                    elif dedupe_blits and (event.get("duplicate_blit") or event.get("duplicate_draw")):
                        if event.get("duplicate_blit"):
                            row["duplicate_blits_skipped"] = int(row.get("duplicate_blits_skipped", 0)) + 1
                        if event.get("duplicate_draw"):
                            row["duplicate_draws_skipped"] = int(row.get("duplicate_draws_skipped", 0)) + 1
                    else:
                        row["events"].append(event)
                    self.gdb_register_read_count += 16
                    if pc not in active:
                        break
                    try:
                        _gdb_remove_breakpoint(self.gdb_sock, pc)
                        if pc in inserted:
                            inserted.remove(pc)
                        event["step_stop"] = self._step_paused_locked()
                        if time.time() < deadline:
                            reply = _gdb_insert_breakpoint(self.gdb_sock, pc)
                            if reply == "OK":
                                inserted.append(pc)
                            else:
                                event["reinsert_error"] = reply
                                break
                    except Exception as exc:
                        event["step_error"] = f"{type(exc).__name__}: {exc}"
                        break
                    if trigger_touch is not None and row.get("trigger_touch") and not row["trigger_touch"].get("up_sent"):
                        hold_seconds = float(row["trigger_touch"].get("hold_seconds", 0.0))
                        if hold_seconds <= 0 or time.time() >= deadline - hold_seconds:
                            pass
                if trigger_touch is not None and row.get("trigger_touch"):
                    x, y = trigger_touch
                    raw_x, raw_y = self._touch_panel_to_adc(x, y)
                    if self.bbk_input_sock is not None:
                        row["trigger_touch"]["up_sent"] = self._send_bbk_input_locked(
                            f"T {x} {y} {raw_x} {raw_y} 0"
                        )
                row["handled_count"] = len(row["events"])
                self.last_gdb_error = None
                return row
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                row["error"] = self.last_gdb_error
                return row
            finally:
                for pc in list(inserted):
                    try:
                        _gdb_remove_breakpoint(self.gdb_sock, pc)
                    except Exception:
                        pass
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                if trigger_thread is not None and trigger_thread_started:
                    trigger_thread.join(timeout=1.0)

    def guest_display_surface_snapshot(self) -> dict[str, object]:
        """Read active GUI surface descriptors used by C200 drawing."""

        out: dict[str, object] = {}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return {"error": "QEMU process is not running"}
            if self.gdb_sock is None:
                return {"error": "QEMU GDB stub is not connected"}
            try:
                self._pause_for_gdb_locked()
                if self.config.machine.lower() == "bbk9588":
                    out["lcd_scanout_source"] = "jz4740-lcd-descriptor"
                active = self._read_u32_paused_locked(0x80474048) or 0
                out["active_object_80474048"] = f"0x{active:08x}"
                if self._is_guest_ram_va(active, 0xD0):
                    obj = self._read_virtual_memory_paused_locked(active, 0xD0)
                    surface = struct.unpack_from("<I", obj, 0x10)[0]
                    out["active_object_words_00_3c"] = [
                        f"0x{struct.unpack_from('<I', obj, off)[0]:08x}"
                        for off in range(0, 0x40, 4)
                    ]
                    out["active_surface_ptr_10"] = f"0x{surface:08x}"
                    pointer_candidates: list[dict[str, object]] = []
                    surface_candidates: list[dict[str, object]] = []
                    for off in range(0, len(obj), 4):
                        value = struct.unpack_from("<I", obj, off)[0]
                        if not self._is_guest_ram_va(value, 0x10):
                            continue
                        row: dict[str, object] = {
                            "offset": f"0x{off:02x}",
                            "value": f"0x{value:08x}",
                        }
                        pointer_candidates.append(row)
                        if self._is_guest_ram_va(value, 0x80):
                            try:
                                candidate = self._read_virtual_memory_paused_locked(value, 0x80)
                                pitch = struct.unpack_from("<I", candidate, 0x18)[0]
                                buffer_va = struct.unpack_from("<I", candidate, 0x44)[0]
                                if pitch and self._is_guest_ram_va(buffer_va, min(0x200, max(1, pitch))):
                                    row["surface_like"] = True
                                    surface_row = self._read_virtual_memory_paused_locked(
                                        buffer_va, min(0x200, max(1, pitch))
                                    )
                                    pixels = [
                                        struct.unpack_from("<H", surface_row, pix_off)[0]
                                        for pix_off in range(0, len(surface_row) & ~1, 2)
                                    ]
                                    surface_candidates.append(
                                        {
                                            "object_offset": f"0x{off:02x}",
                                            "surface": f"0x{value:08x}",
                                            "width": struct.unpack_from("<I", candidate, 0x00)[0],
                                            "height": struct.unpack_from("<I", candidate, 0x04)[0],
                                            "pitch": pitch,
                                            "buffer_va_44": f"0x{buffer_va:08x}",
                                            "row0_unique": len(set(pixels)),
                                            "row0_nonzero": sum(1 for pixel in pixels if pixel),
                                        }
                                    )
                            except Exception as exc:
                                row["surface_probe_error"] = f"{type(exc).__name__}: {exc}"
                    out["active_object_pointer_candidates"] = pointer_candidates
                    out["active_surface_candidates"] = surface_candidates
                    child_objects: list[dict[str, object]] = []
                    for row in pointer_candidates:
                        try:
                            value = int(str(row["value"]), 16)
                        except Exception:
                            continue
                        if not self._is_guest_ram_va(value, 0x40):
                            continue
                        try:
                            child = self._read_virtual_memory_paused_locked(value, 0x40)
                        except Exception as exc:
                            child_objects.append(
                                {
                                    "from_offset": row["offset"],
                                    "object": row["value"],
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                            )
                            continue
                        child_objects.append(
                            {
                                "from_offset": row["offset"],
                                "object": row["value"],
                                "words_00_3c": [
                                    f"0x{struct.unpack_from('<I', child, child_off)[0]:08x}"
                                    for child_off in range(0, 0x40, 4)
                                ],
                            }
                        )
                    out["active_child_object_candidates"] = child_objects
                    if self._is_guest_ram_va(surface, 0x80):
                        surface_data = self._read_virtual_memory_paused_locked(surface, 0x80)
                        pitch = struct.unpack_from("<I", surface_data, 0x18)[0]
                        buffer_va = struct.unpack_from("<I", surface_data, 0x44)[0]
                        vtable = struct.unpack_from("<I", surface_data, 0x6C)[0]
                        out["active_surface"] = {
                            "addr": f"0x{surface:08x}",
                            "width": struct.unpack_from("<I", surface_data, 0x00)[0],
                            "height": struct.unpack_from("<I", surface_data, 0x04)[0],
                            "pitch": pitch,
                            "buffer_va_44": f"0x{buffer_va:08x}",
                            "vtable_6c": f"0x{vtable:08x}",
                            "words_00_7c": [
                                f"0x{struct.unpack_from('<I', surface_data, off)[0]:08x}"
                                for off in range(0, 0x80, 4)
                            ],
                        }
                        if self._is_guest_ram_va(buffer_va, min(0x200, max(0, pitch))):
                            row = self._read_virtual_memory_paused_locked(buffer_va, min(0x200, max(1, pitch)))
                            pixels = [
                                struct.unpack_from("<H", row, off)[0]
                                for off in range(0, len(row) & ~1, 2)
                            ]
                            out["active_surface_row0_unique"] = len(set(pixels))
                            out["active_surface_row0_nonzero"] = sum(1 for value in pixels if value)
                frame_stats: dict[str, object] = {}
                for name, addr in (
                    ("lcd_fb_81f82000", 0x81F82000),
                    ("portrait_src_80825b90", 0x80825B90),
                    ("active_pixels_809660f0", 0x809660F0),
                    ("temp_surface_80cee7fc", 0x80CEE7FC),
                    ("maybe_surface_80965378", 0x80965378),
                ):
                    try:
                        if self._is_guest_ram_va(addr, 240 * 320 * 2):
                            data = self._read_virtual_memory_chunked_paused_locked(addr, 240 * 320 * 2)
                            frame_stats[name] = self._rgb565_frame_stats(data)
                    except Exception as exc:
                        frame_stats[name] = {"error": f"{type(exc).__name__}: {exc}"}
                out["frame_candidate_stats"] = frame_stats
                self.gdb_read_count += 1
                self.last_gdb_error = None
                return out
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                return {"error": self.last_gdb_error}
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def enable_lcd_mirror(self) -> dict[str, object]:
        """Retain the legacy API while descriptor scanout needs no host action."""

        row: dict[str, object] = {"event": "qemu-enable-lcd-mirror", "enabled": False}
        if self.config.machine.lower() == "bbk9588":
            row.update(
                {
                    "enabled": True,
                    "skipped": True,
                    "reason": "jz4740-lcd-descriptor-scanout",
                    "source": "jz4740-lcd",
                }
            )
            return row
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                row["error"] = "QEMU process is not running"
                return row
            if self.gdb_sock is None:
                row["error"] = "QEMU GDB stub is not connected"
                return row
            try:
                self._pause_for_gdb_locked()
                before = self._read_u32_paused_locked(0x80474040) or 0
                config = self._read_virtual_memory_paused_locked(0x804A6B88, 0xE0)
                width = struct.unpack_from("<H", config, 0x00)[0]
                height = struct.unpack_from("<H", config, 0x04)[0]
                fb = struct.unpack_from("<I", config, 0xD8)[0]
                reverse = config[0xDC]
                row.update(
                    {
                        "before": f"0x{before:08x}",
                        "width": width,
                        "height": height,
                        "fb": f"0x{fb:08x}",
                        "reverse": bool(reverse),
                    }
                )
                if width != 240 or height != 320 or fb == 0:
                    row["error"] = "lcd mirror config is not initialized"
                    return row
                self._write_u32_paused_locked(0x80474040, 1)
                self.gdb_write_count += 1
                after = self._read_u32_paused_locked(0x80474040) or 0
                row["after"] = f"0x{after:08x}"
                row["enabled"] = after != 0
                self.last_gdb_error = None
                return row
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                row["error"] = self.last_gdb_error
                return row
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def settle_initial_gui(self) -> dict[str, object]:
        return {
            "event": "qemu-settle-initial-gui",
            "disabled": True,
            "reason": "Python/GDB settle services were removed from the hardware-model path",
            "legacy_python_storage_hook": {
                "event": "qemu-legacy-python-storage-hook",
                "disabled": True,
                "handled_count": 0,
            },
            "legacy_python_resource_hook": {
                "event": "qemu-legacy-python-resource-hook",
                "disabled": True,
                "handled_count": 0,
            },
        }

    def apply_gui_key_event(self, code: int, down: bool = True) -> dict[str, object]:
        """Apply one GUI key event through the QEMU input path."""

        key_code = int(code) & 0xFF
        down = bool(down)
        table_entry_va = 0x806C5D10 + key_code * 4
        row: dict[str, object] = {
            "event": "qemu-gui-key",
            "code": key_code,
            "down": down,
            "table_entry": f"0x{table_entry_va:08x}",
            "applied": False,
        }
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                row["error"] = "QEMU process is not running"
                return row
            if self.config.machine.lower() == "bbk9588" and self.bbk_input_sock is not None:
                sent = self._send_bbk_input_locked(f"K {key_code} {1 if down else 0}")
                row.update(
                    {
                        "applied": sent,
                        "source": "qemu-c-machine-chardev",
                        "bbk_input_connected": self.bbk_input_sock is not None,
                        "bbk_input_write_count": self.bbk_input_write_count,
                        "last_bbk_input_error": self.last_bbk_input_error,
                        "mailbox": None,
                    }
                )
                if not sent:
                    row["error"] = self.last_bbk_input_error or "key event was not sent to QEMU C machine"
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            if self.config.machine.lower() == "bbk9588":
                row.update(
                    {
                        "source": "qemu-c-machine-chardev",
                        "bbk_input_connected": False,
                        "bbk_input_write_count": self.bbk_input_write_count,
                        "last_bbk_input_error": self.last_bbk_input_error,
                        "mailbox": None,
                        "mailbox_seq": None,
                        "gui_idle_pump": {
                            "source": "qemu-c-machine",
                            "skipped": True,
                            "reason": "bbk9588-input-chardev-unavailable",
                        },
                    }
                )
                row["error"] = "QEMU input chardev is not connected; refusing guest-RAM mailbox/global fallback"
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            if self.gdb_sock is None:
                row["error"] = "QEMU GDB stub is not connected"
                return row
            try:
                self._pause_for_gdb_locked()
                node_va = self._read_u32_paused_locked(table_entry_va) or 0
                row["node"] = f"0x{node_va:08x}"
                if not (0x806C0000 <= node_va < 0x80700000):
                    row["error"] = "missing-key-table-node"
                    return row
                slot = self._read_u8_paused_locked(node_va + 0x37)
                mask = self._read_u8_paused_locked(node_va + 0x38)
                group_mask = self._read_u8_paused_locked(node_va + 0x39)
                old_flags = self._read_u8_paused_locked(node_va + 0x34)
                old_gate = self._read_u8_paused_locked(0x80473F08)
                if slot is None or mask is None or group_mask is None or old_flags is None or old_gate is None:
                    row["error"] = "unreadable-key-node"
                    return row
                input_byte_va = 0x80473F40 + slot
                input_group_va = 0x80473F38
                old_input = self._read_u8_paused_locked(input_byte_va)
                old_group = self._read_u8_paused_locked(input_group_va)
                if old_input is None or old_group is None:
                    row["error"] = "unreadable-input-state"
                    return row
                if down:
                    new_input = old_input | mask
                    new_group = old_group | group_mask
                    new_flags = old_flags | 0x08
                else:
                    new_input = old_input & ~mask
                    new_group = old_group & ~group_mask
                    new_flags = old_flags & ~0x08
                self._write_u8_paused_locked(input_byte_va, new_input)
                self._write_u8_paused_locked(input_group_va, new_group)
                self._write_u8_paused_locked(node_va + 0x34, new_flags)
                self._write_u8_paused_locked(0x80473F08, 0)
                row["gui_idle_pump"] = self._pump_gui_idle_dispatcher_paused_locked()
                row.update(
                    {
                        "applied": True,
                        "slot": slot,
                        "mask": f"0x{mask:02x}",
                        "group_mask": f"0x{group_mask:02x}",
                        "input_va": f"0x{input_byte_va:08x}",
                        "input_old": f"0x{old_input:02x}",
                        "input_new": f"0x{new_input:02x}",
                        "group_old": f"0x{old_group:02x}",
                        "group_new": f"0x{new_group:02x}",
                        "flags_old": f"0x{old_flags:02x}",
                        "flags_new": f"0x{new_flags:02x}",
                        "gate_80473f08_old": f"0x{old_gate:02x}",
                        "gate_80473f08_new": "0x00",
                    }
                )
                self.gdb_write_count += 1
                self.last_gdb_error = None
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                row["error"] = self.last_gdb_error
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def apply_touch_state(self, x: int, y: int, down: bool) -> dict[str, object]:
        """Apply one touch state through the QEMU input path."""

        x = max(0, min(239, int(x)))
        y = max(0, min(319, int(y)))
        down = bool(down)
        raw_x, raw_y = self._touch_panel_to_adc(x, y)
        row: dict[str, object] = {
            "event": "qemu-touch-state",
            "x": x,
            "y": y,
            "down": down,
            "raw_x": f"0x{raw_x:03x}",
            "raw_y": f"0x{raw_y:03x}",
            "applied": False,
        }
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                row["error"] = "QEMU process is not running"
                return row
            if self.config.machine.lower() == "bbk9588" and self.bbk_input_sock is not None:
                sent = self._send_bbk_input_locked(f"T {x} {y} {raw_x} {raw_y} {1 if down else 0}")
                row.update(
                    {
                        "applied": sent,
                        "source": "qemu-c-machine-chardev",
                        "bbk_input_connected": self.bbk_input_sock is not None,
                        "bbk_input_write_count": self.bbk_input_write_count,
                        "last_bbk_input_error": self.last_bbk_input_error,
                        "mailbox": None,
                        "firmware_globals_written": False,
                        "calibration_release_seeded": False,
                        "gui_handler": {
                            "source": "qemu-c-machine",
                            "called": False,
                            "skipped": True,
                            "reason": "bbk9588-input-chardev",
                        },
                    }
                )
                if not sent:
                    row["error"] = self.last_bbk_input_error or "touch event was not sent to QEMU C machine"
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            if self.config.machine.lower() == "bbk9588":
                row.update(
                    {
                        "source": "qemu-c-machine-chardev",
                        "bbk_input_connected": False,
                        "bbk_input_write_count": self.bbk_input_write_count,
                        "last_bbk_input_error": self.last_bbk_input_error,
                        "mailbox": None,
                        "mailbox_seq": None,
                        "firmware_globals_written": False,
                        "calibration_release_seeded": False,
                        "gui_handler": {
                            "source": "qemu-c-machine",
                            "called": False,
                            "skipped": True,
                            "reason": "bbk9588-input-chardev-unavailable",
                        },
                    }
                )
                row["error"] = "QEMU input chardev is not connected; refusing guest-RAM mailbox/global fallback"
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            if self.gdb_sock is None:
                row["error"] = "QEMU GDB stub is not connected"
                return row
            try:
                self._pause_for_gdb_locked()
                pc = self._read_pc_paused_locked()
                prev_x = self._read_u32_paused_locked(0x80370FC8)
                prev_y = self._read_u32_paused_locked(0x80370FCC)
                self._write_u8_paused_locked(0x807F7110, 1 if down else 0)
                self._write_u16_paused_locked(0x807F7112, raw_x)
                self._write_u16_paused_locked(0x807F7114, raw_y)
                self._write_u32_paused_locked(0x80370FC0, x if prev_x is None else prev_x)
                self._write_u32_paused_locked(0x80370FC4, y if prev_y is None else prev_y)
                self._write_u32_paused_locked(0x80370FC8, x)
                self._write_u32_paused_locked(0x80370FCC, y)
                self._write_u16_paused_locked(0x807F7116, x)
                self._write_u16_paused_locked(0x807F7118, y)
                self._write_u32_paused_locked(0x80370FD0, 0 if down else 1)
                self._write_u32_paused_locked(0x80370FD4, 0x7F)
                self._write_u32_paused_locked(0x8048DD00, 0 if down else 1)
                self._write_u32_paused_locked(0x8048DD04, 1 if down else 0)
                self._write_u32_paused_locked(0x8048DD08, 0)
                calibration_release_seeded = False
                if not down and pc is not None and 0x80017000 <= pc <= 0x80019300:
                    self._write_u8_paused_locked(0x80477D84, 1)
                    self._write_u32_paused_locked(0x80362794, 0x28)
                    calibration_release_seeded = True
                row.update(
                    {
                        "applied": True,
                        "pc": self._format_u32(pc),
                        "prev_x": self._format_u32(prev_x),
                        "prev_y": self._format_u32(prev_y),
                        "touch_x_addr": "0x80370fc8",
                        "touch_y_addr": "0x80370fcc",
                        "latch_addr": "0x807f7110",
                        "calibration_release_seeded": calibration_release_seeded,
                    }
                )
                active_override = self.touch_capture_active if not down else None
                row["gui_handler"] = self._call_gui_touch_handler_paused_locked(x, y, down, active_override=active_override)
                handler_active_s = row["gui_handler"].get("active") if isinstance(row.get("gui_handler"), dict) else None
                try:
                    handler_active = int(str(handler_active_s), 16) if handler_active_s is not None else 0
                except Exception:
                    handler_active = 0
                if down and handler_active:
                    self.touch_capture_active = handler_active
                elif not down:
                    row["touch_capture_active"] = self._format_u32(self.touch_capture_active)
                    self.touch_capture_active = None
                row["gui_ring_pump"] = self._pump_gui_ring_once_paused_locked()
                row["gui_idle_pump"] = self._pump_gui_idle_dispatcher_paused_locked()
                if not down:
                    row["gui_modal_close_settle"] = self._settle_gui_modal_close_paused_locked()
                    row["gui_event_poller"] = self._pump_gui_event_poller_paused_locked()
                    row["scheduler_ready_seed"] = self._seed_scheduler_ready_task_paused_locked(9)
                    row["scheduler_dispatch_snapshot_after_seed"] = self._scheduler_dispatch_snapshot_paused_locked()
                    row["scheduler_dispatch_after_seed"] = self._pump_gui_idle_dispatcher_paused_locked()
                    row["scheduler_dispatch_snapshot_after_countdown"] = self._scheduler_dispatch_snapshot_paused_locked()
                    row["scheduled_fs_scan_task_service"] = self._service_scheduled_fs_scan_task_paused_locked(
                        9, timeout=3.0, max_hits=512
                    )
                    row["scheduler_dispatch_after_countdown"] = self._pump_gui_idle_dispatcher_paused_locked()
                    row["legacy_python_storage_hook"] = self._service_legacy_python_storage_hooks_paused_locked(timeout=3.0, max_hits=512)
                    row["legacy_python_resource_hook"] = self._service_legacy_python_resource_hook_rounds_paused_locked(
                        rounds=4, timeout_per_round=2.0, max_hits_per_round=512
                    )
                    row["fs_scan_probe"] = self._service_fs_scan_probe_paused_locked(timeout=3.0, max_hits=512)
                    row["first_root_directory_scan_probe"] = self._service_first_root_directory_scan_probe_paused_locked(
                        timeout=3.0, max_hits=768
                    )
                    row["first_child_directory_scan_probe"] = self._service_first_child_directory_scan_probe_paused_locked(
                        timeout=3.0, max_hits=1024
                    )
                    row["first_file_open_probe"] = self._service_first_file_open_probe_paused_locked(
                        timeout=3.0, max_hits=1024
                    )
                    row["first_file_high_level_open_probe"] = (
                        self._service_first_file_high_level_open_probe_paused_locked(timeout=3.0, max_hits=1280)
                    )
                    row["legacy_python_storage_hook_after_resource_hook"] = self._service_legacy_python_storage_hooks_paused_locked(
                        timeout=2.0, max_hits=512
                    )
                    row["gui_repaint_settle"] = self._settle_gui_repaint_paused_locked()
                self.gdb_write_count += 1
                self.last_gdb_error = None
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            except Exception as exc:
                self.last_gdb_error = f"{type(exc).__name__}: {exc}"
                row["error"] = self.last_gdb_error
                self.guest_input_events.append(row)
                del self.guest_input_events[:-32]
                return row
            finally:
                try:
                    self._resume_after_gdb_locked()
                except Exception as exc:
                    self.last_gdb_error = f"{type(exc).__name__}: {exc}"

    def _call_gui_touch_handler_paused_locked(
        self,
        x: int,
        y: int,
        down: bool,
        *,
        active_override: int | None = None,
    ) -> dict[str, object]:
        """Try to invoke the GUI touch handler if an active GUI object exists."""

        active = active_override if active_override else (self._read_u32_paused_locked(0x80474048) or 0)
        out: dict[str, object] = {
            "handler": "0x800dd380",
            "active": f"0x{active:08x}",
            "called": False,
        }
        if active == 0 or not self._is_guest_ram_va(active, 0x90):
            out["error"] = "missing-active-object"
            return out
        left = self._read_u32_paused_locked(active + 4) or 0
        top = self._read_u32_paused_locked(active + 8) or 0
        local_x = max(0, min(0xFFFF, int(x) - left))
        local_y = max(0, min(0xFFFF, int(y) - top))
        event_type = 1 if down else 2
        packed = (local_x & 0xFFFF) | ((local_y & 0xFFFF) << 16)
        out.update(
            {
                "left": left,
                "top": top,
                "local_x": local_x,
                "local_y": local_y,
                "event_type": event_type,
                "packed": f"0x{packed:08x}",
            }
        )
        call = self._call_guest_function_stepped_paused_locked(
            0x800DD380,
            args=(active, event_type, 0, packed),
            return_pc=0x80008A8C,
            max_steps=2048,
            max_recorded_steps=24,
            continue_timeout=2.0,
        )
        out["call"] = call
        out["called"] = bool(call.get("returned"))
        if not call.get("returned"):
            out["error"] = call.get("error") or "guest handler did not return"
        return out

    def _pump_gui_ring_once_paused_locked(self, queue_va: int = 0x80825840) -> dict[str, object]:
        """Pump one GUI/display ring record using the firmware event semantics."""

        out: dict[str, object] = {
            "event": "qemu-gui-ring-pump",
            "queue": f"0x{queue_va:08x}",
            "called": False,
            "pumped": False,
        }
        if not self._is_guest_ram_va(queue_va, 0x20):
            out["error"] = "queue-not-mapped"
            return out
        buffer_va = self._read_u32_paused_locked(queue_va + 0x10) or 0
        capacity = self._read_u32_paused_locked(queue_va + 0x14) or 0
        read_idx = self._read_u32_paused_locked(queue_va + 0x18) or 0
        write_idx = self._read_u32_paused_locked(queue_va + 0x1C) or 0
        out.update(
            {
                "buffer": f"0x{buffer_va:08x}",
                "capacity": capacity,
                "read_old": read_idx,
                "write": write_idx,
            }
        )
        if capacity <= 0 or capacity > 0x100:
            out["error"] = "invalid-capacity"
            return out
        if read_idx == write_idx:
            out["reason"] = "empty"
            return out
        read_idx %= capacity
        write_idx %= capacity
        record_size = 0x1C
        record_va = buffer_va + read_idx * record_size
        if not self._is_guest_ram_va(record_va, record_size):
            out["error"] = "record-not-mapped"
            out["record"] = f"0x{record_va:08x}"
            return out
        obj = self._read_u32_paused_locked(record_va) or 0
        gui_event = self._read_u32_paused_locked(record_va + 4) or 0
        next_idx = (read_idx + 1) % capacity
        self._write_u32_paused_locked(queue_va + 0x18, next_idx)
        if next_idx == write_idx:
            flags = self._read_u32_paused_locked(queue_va) or 0
            self._write_u32_paused_locked(queue_va, flags & ~0x40000000)
            out["flags_old"] = f"0x{flags:08x}"
            out["flags_new"] = f"0x{flags & ~0x40000000:08x}"
        out.update(
            {
                "pumped": True,
                "record": f"0x{record_va:08x}",
                "object": f"0x{obj:08x}",
                "gui_event": f"0x{gui_event:08x}",
                "read_new": next_idx,
            }
        )
        if obj in (0, 0xFFFFFFFF):
            out["reason"] = "empty-object"
            return out
        call = self._call_guest_function_stepped_paused_locked(
            0x800DD4B8,
            args=(record_va, 0, 0, 0),
            return_pc=0x80008A8C,
            max_steps=2048,
            max_recorded_steps=24,
            continue_timeout=2.0,
        )
        out["call"] = call
        out["called"] = bool(call.get("returned"))
        if not call.get("returned"):
            out["error"] = call.get("error") or "gui ring pump handler did not return"
        return out

    def _clamp_scheduler_tick_paused_locked(self) -> dict[str, object]:
        """Mirror the scheduler tick clamp before forcing a dispatcher call."""

        if self.config.machine.lower() == "bbk9588":
            return {
                "event": "qemu-scheduler-tick-clamp",
                "clamped": True,
                "skipped": True,
                "reason": "qemu-c-machine-scheduler-tick-clamp",
                "source": "qemu-c-machine",
            }
        enabled = self._read_u8_paused_locked(0x80473F09) or 0
        countdown = self._read_u8_paused_locked(0x80473F08) or 0
        delay = self._read_u8_paused_locked(0x80473F4D) or 0
        out: dict[str, object] = {
            "event": "qemu-scheduler-tick-clamp",
            "enabled": enabled,
            "countdown_before": countdown,
            "delay_before": delay,
            "clamped": False,
        }
        if enabled == 1 and (countdown != 0 or delay != 0):
            self._write_u8_paused_locked(0x80473F08, 0)
            self._write_u8_paused_locked(0x80473F4D, 0)
            out["clamped"] = True
            out["countdown_after"] = 0
            out["delay_after"] = 0
            self.gdb_write_count += 1
        return out

    def _seed_scheduler_ready_task_paused_locked(self, task_id: int) -> dict[str, object]:
        """Mark a scheduler task ready when synthesized GUI dispatch skipped the firmware wakeup edge."""

        task = int(task_id) & 0xFF
        if self.config.machine.lower() == "bbk9588" and task == 9:
            return {
                "event": "qemu-scheduler-ready-seed",
                "task": "0x09",
                "seeded": True,
                "skipped": True,
                "reason": "qemu-c-machine-scheduler-ready-task9",
                "source": "qemu-c-machine",
            }
        group = task >> 3
        bit = task & 7
        group_addr = 0x80473F38
        byte_addr = 0x80473F40 + group
        current_task = self._read_u8_paused_locked(0x80473F10) or 0
        last_task = self._read_u8_paused_locked(0x80473F11) or 0
        active_node = self._read_u32_paused_locked(0x80473F30) or 0
        target_node = self._read_u32_paused_locked(0x806C5D10 + task * 4) or 0
        group_before = self._read_u8_paused_locked(group_addr) or 0
        byte_before = self._read_u8_paused_locked(byte_addr) or 0
        countdown_before = self._read_u8_paused_locked(0x80473F08) or 0
        out: dict[str, object] = {
            "event": "qemu-scheduler-ready-seed",
            "task": f"0x{task:02x}",
            "group": group,
            "bit": bit,
            "current_task_before": f"0x{current_task:02x}",
            "last_task_before": f"0x{last_task:02x}",
            "active_node_before": f"0x{active_node:08x}",
            "target_node": f"0x{target_node:08x}",
            "group_before": f"0x{group_before:02x}",
            "byte_before": f"0x{byte_before:02x}",
            "countdown_before": f"0x{countdown_before:02x}",
            "seeded": False,
        }
        if task == 0x3F:
            out["reason"] = "idle-task"
            return out
        if target_node == 0 or not self._is_guest_ram_va(target_node, 0x80):
            out["reason"] = "missing-target-task"
            return out
        group_after = group_before | (1 << group)
        byte_after = byte_before | (1 << bit)
        self._write_u8_paused_locked(group_addr, group_after)
        self._write_u8_paused_locked(byte_addr, byte_after)
        if countdown_before == 0:
            self._write_u8_paused_locked(0x80473F08, 1)
        self.gdb_write_count += 1
        out.update(
            {
                "seeded": True,
                "group_after": f"0x{group_after:02x}",
                "byte_after": f"0x{byte_after:02x}",
                "countdown_after": f"0x{(1 if countdown_before == 0 else countdown_before):02x}",
            }
        )
        return out

    def _handle_scheduler_dispatch_task_node_paused_locked(self, pc: int) -> dict[str, object]:
        node = self._read_register_paused_locked(2) & 0xFFFFFFFF
        row: dict[str, object] = {
            "pc": f"0x{pc & 0xFFFFFFFF:08x}",
            "kind": "scheduler-dispatch-task-node",
            "handled": False,
            "node": f"0x{node:08x}",
        }
        self.gdb_register_read_count += 1
        if not self._is_guest_ram_va(node, 0x40):
            self._write_register_paused_locked(37, 0x800081B8)
            self.gdb_register_write_count += 1
            row.update(
                {
                    "handled": True,
                    "reason": "missing-task-node-return-dispatcher",
                    "return_pc": "0x800081b8",
                }
            )
            return row
        row["handled"] = True
        row["reason"] = "task-node-valid-step"
        row["step_stop"] = self._step_paused_locked()
        return row

    def _sanitize_scheduler_ready_bits_paused_locked(self) -> dict[str, object]:
        out: dict[str, object] = {
            "event": "qemu-scheduler-ready-sanitize",
            "changed": False,
            "cleared_tasks": [],
        }
        if self.config.machine.lower() == "bbk9588":
            out.update(
                {
                    "skipped": True,
                    "reason": "qemu-c-machine-scheduler-ready-sanitize",
                    "source": "qemu-c-machine",
                }
            )
            return out
        group_mask = self._read_u8_paused_locked(0x80473F38)
        if group_mask is None:
            out["error"] = "missing-group-mask"
            return out
        out["group_before"] = f"0x{group_mask:02x}"
        group_after = group_mask
        for group in range(8):
            if not (group_mask & (1 << group)):
                continue
            byte_addr = 0x80473F40 + group
            ready = self._read_u8_paused_locked(byte_addr)
            if ready is None:
                out["error"] = f"missing-ready-byte-{group}"
                return out
            ready_after = ready
            for bit in range(8):
                if not (ready & (1 << bit)):
                    continue
                task = group * 8 + bit
                node = self._read_u32_paused_locked(0x806C5D10 + task * 4) or 0
                if node and self._is_guest_ram_va(node, 0x40):
                    continue
                ready_after &= ~(1 << bit)
                out["cleared_tasks"].append(f"0x{task:02x}")  # type: ignore[union-attr]
            if ready_after != ready:
                self._write_u8_paused_locked(byte_addr, ready_after)
                self.gdb_write_count += 1
                out["changed"] = True
            if ready_after == 0:
                group_after &= ~(1 << group)
        if group_after != group_mask:
            self._write_u8_paused_locked(0x80473F38, group_after)
            self.gdb_write_count += 1
            out["changed"] = True
        out["group_after"] = f"0x{group_after:02x}"
        return out

    def _scheduler_dispatch_snapshot_paused_locked(self) -> dict[str, object]:
        row: dict[str, object] = {
            "event": "qemu-scheduler-dispatch-snapshot",
            "computed": False,
        }
        try:
            fields = self._read_virtual_memory_paused_locked(0x80473F08, 0x50)
            order_table = self._read_virtual_memory_paused_locked(0x8024A998, 0x100)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            return row
        countdown = fields[0]
        enabled = fields[1]
        current_task = fields[8]
        last_task = fields[9]
        current_slot = fields[0x30]
        delay = fields[0x45]
        row.update(
            {
                "enabled": f"0x{enabled:02x}",
                "countdown": f"0x{countdown:02x}",
                "delay": f"0x{delay:02x}",
                "current_task": f"0x{current_task:02x}",
                "last_task": f"0x{last_task:02x}",
                "current_slot": f"0x{current_slot:02x}",
            }
        )
        if enabled != 1 or delay != 0 or countdown != 0 or current_slot >= len(order_table):
            row["reason"] = "scheduler-not-dispatchable"
            return row
        table_index = order_table[current_slot]
        queue_offset = 0x38 + table_index
        if queue_offset >= len(fields):
            row["reason"] = "queue-offset-out-of-range"
            return row
        next_slot = fields[queue_offset]
        if next_slot >= len(order_table):
            row["reason"] = "next-slot-out-of-range"
            return row
        next_task = (order_table[next_slot] + (table_index << 3)) & 0xFF
        target_node = self._read_u32_paused_locked(0x806C5D10 + next_task * 4) or 0
        row.update(
            {
                "computed": True,
                "table_index": f"0x{table_index:02x}",
                "queue_offset": f"0x{queue_offset:02x}",
                "next_slot": f"0x{next_slot:02x}",
                "next_task": f"0x{next_task:02x}",
                "target_node": f"0x{target_node:08x}",
                "same_as_last": next_task == last_task,
            }
        )
        return row

    def _find_gui_timer_event_slot_paused_locked(self, event_obj: int, owner_obj: int, timer_id: int) -> int | None:
        slot_objects_offset = 0x20
        slot_ids_offset = 0x60
        slots = 16
        size = slot_ids_offset + slots * 4
        if not self._is_guest_ram_va(event_obj, size):
            return None
        try:
            data = self._read_virtual_memory_paused_locked(event_obj, size)
        except Exception:
            return None
        for slot in range(slots):
            slot_obj = struct.unpack_from("<I", data, slot_objects_offset + slot * 4)[0]
            slot_id = struct.unpack_from("<I", data, slot_ids_offset + slot * 4)[0]
            if slot_obj == owner_obj and slot_id == timer_id:
                return slot
        return None

    def _service_gui_timer_entries_paused_locked(self) -> dict[str, object]:
        table_va = 0x804A6B40
        slots = 16
        event_object_offset = 0xF0
        out: dict[str, object] = {
            "event": "qemu-gui-timer-service",
            "table": f"0x{table_va:08x}",
            "entries": [],
            "fired": 0,
        }
        if self.config.machine.lower() == "bbk9588":
            self.gui_timer_tick_count += 1
            out.update(
                {
                    "skipped": True,
                    "reason": "qemu-c-machine-gui-timer-service",
                    "source": "qemu-c-machine",
                }
            )
            return out
        if not self._is_guest_ram_va(table_va, slots * 4):
            out["error"] = "timer table is outside guest RAM"
            return out
        self.gui_timer_tick_count += 1
        try:
            table = self._read_virtual_memory_paused_locked(table_va, slots * 4)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        for index in range(slots):
            entry = struct.unpack_from("<I", table, index * 4)[0]
            if not self._is_guest_ram_va(entry, 0x10):
                continue
            try:
                entry_data = self._read_virtual_memory_paused_locked(entry, 0x10)
            except Exception:
                continue
            owner_obj, timer_id, period, counter = struct.unpack_from("<IIII", entry_data)
            if not self._is_guest_ram_va(owner_obj, event_object_offset + 4):
                continue
            period = max(1, period)
            counter = (counter + 1) & 0xFFFFFFFF
            row: dict[str, object] = {
                "index": index,
                "entry": f"0x{entry:08x}",
                "owner_obj": f"0x{owner_obj:08x}",
                "timer_id": f"0x{timer_id:08x}",
                "period": period,
                "counter_after_increment": counter,
            }
            if counter < period:
                self._write_u32_paused_locked(entry + 0x0C, counter)
                row["fired"] = False
                out["entries"].append(row)
                self.gui_timer_events.append(row)
                continue
            self._write_u32_paused_locked(entry + 0x0C, 0)
            event_obj = self._read_u32_paused_locked(owner_obj + event_object_offset) or 0
            slot = self._find_gui_timer_event_slot_paused_locked(event_obj, owner_obj, timer_id)
            row["event_obj"] = f"0x{event_obj:08x}"
            row["slot"] = slot
            if slot is None:
                row["fired"] = False
                row["reason"] = "missing-event-slot"
                out["entries"].append(row)
                self.gui_timer_events.append(row)
                continue
            flags = self._read_u32_paused_locked(event_obj) or 0
            new_flags = flags | (1 << slot)
            self._write_u32_paused_locked(event_obj, new_flags)
            self.gui_timer_fire_count += 1
            row.update(
                {
                    "fired": True,
                    "flags_before": f"0x{flags:08x}",
                    "flags_after": f"0x{new_flags:08x}",
                }
            )
            out["fired"] = int(out["fired"]) + 1
            out["entries"].append(row)
            self.gui_timer_events.append(row)
        del self.gui_timer_events[:-128]
        if out["entries"]:
            self.gdb_write_count += 1
        return out

    def _pump_gui_idle_dispatcher_paused_locked(self) -> dict[str, object]:
        """Run the GUI idle dispatcher after injected GUI events."""

        if self.config.machine.lower() == "bbk9588":
            row = self._bbk9588_python_guest_service_disabled("qemu-gui-idle-pump")
            row.update(
                {
                    "handler": "0x80007e08",
                    "dispatcher": "0x800080f0",
                    "called": False,
                }
            )
            return row
        out: dict[str, object] = {
            "event": "qemu-gui-idle-pump",
            "handler": "0x80007e08",
            "dispatcher": "0x800080f0",
            "called": True,
        }
        out["scheduler_ready_sanitize"] = self._sanitize_scheduler_ready_bits_paused_locked()
        out["gui_timer_service"] = self._service_gui_timer_entries_paused_locked()
        call = self._call_guest_function_stepped_paused_locked(
            0x80007E08,
            args=(),
            return_pc=0x80008A8C,
            max_steps=4096,
            max_recorded_steps=24,
            continue_timeout=2.0,
        )
        out["call"] = call
        out["returned"] = bool(call.get("returned"))
        if not call.get("returned"):
            out["error"] = call.get("error") or "GUI idle dispatcher did not return"
        return out

    def _settle_gui_modal_close_paused_locked(self) -> dict[str, object]:
        """Use firmware helpers to clear the GUI busy list that can block modal close."""

        if self.config.machine.lower() == "bbk9588":
            row = self._bbk9588_python_guest_service_disabled("qemu-gui-modal-close-settle")
            row["attempted"] = False
            return row
        out: dict[str, object] = {
            "event": "qemu-gui-modal-close-settle",
            "attempted": False,
        }
        modal_before = self._read_u32_paused_locked(0x804A65C0) or 0
        count_before = self._read_u32_paused_locked(0x80825820) or 0
        head_before = self._read_u32_paused_locked(0x80825828) or 0
        out.update(
            {
                "modal_before": f"0x{modal_before:08x}",
                "busy_count_before": count_before,
                "busy_head_before": f"0x{head_before:08x}",
            }
        )
        if modal_before == 0:
            out["reason"] = "no-modal"
            return out
        if count_before <= 0 or head_before == 0 or not self._is_guest_ram_va(head_before, 8):
            out["reason"] = "no-blocking-busy-node"
            return out
        object_va = self._read_u32_paused_locked(head_before) or 0
        if object_va == 0 or not self._is_guest_ram_va(object_va, 0x90):
            out["reason"] = "invalid-busy-object"
            out["object"] = f"0x{object_va:08x}"
            return out
        out["attempted"] = True
        out["object"] = f"0x{object_va:08x}"
        remove_call = self._call_guest_function_stepped_paused_locked(
            0x800D8820,
            args=(0x80825820, object_va, 0, 0),
            return_pc=0x80008A8C,
            max_steps=512,
            max_recorded_steps=24,
            continue_timeout=1.0,
        )
        out["remove_call"] = remove_call
        count_after_remove = self._read_u32_paused_locked(0x80825820) or 0
        head_after_remove = self._read_u32_paused_locked(0x80825828) or 0
        out["busy_count_after_remove"] = count_after_remove
        out["busy_head_after_remove"] = f"0x{head_after_remove:08x}"
        if not remove_call.get("returned"):
            out["error"] = remove_call.get("error") or "busy-list remove helper did not return"
            return out
        close_call = self._call_guest_function_stepped_paused_locked(
            0x800D3D04,
            args=(0, 0xD2, 0, 0),
            return_pc=0x80008A8C,
            max_steps=4096,
            max_recorded_steps=24,
            continue_timeout=2.0,
        )
        out["close_call"] = close_call
        modal_after = self._read_u32_paused_locked(0x804A65C0) or 0
        out["modal_after"] = f"0x{modal_after:08x}"
        out["closed"] = modal_after == 0
        if not close_call.get("returned"):
            out["error"] = close_call.get("error") or "modal close event did not return"
        return out

    def _pump_gui_event_poller_paused_locked(
        self,
        *,
        scratch_va: int = DEFAULT_QEMU_GUI_EVENT_SCRATCH,
        max_events: int = 4,
    ) -> dict[str, object]:
        """Poll and dispatch GUI events represented by queue flags rather than ring records."""

        if self.config.machine.lower() == "bbk9588":
            row = self._bbk9588_python_guest_service_disabled("qemu-gui-event-poller")
            row.update({"scratch": f"0x{scratch_va:08x}", "events": []})
            return row
        out: dict[str, object] = {
            "event": "qemu-gui-event-poller",
            "scratch": f"0x{scratch_va:08x}",
            "events": [],
        }
        if not self._is_guest_ram_va(scratch_va, 0x1C):
            out["error"] = "scratch-not-mapped"
            return out
        events: list[dict[str, object]] = []
        for index in range(max(0, int(max_events))):
            flags_before = self._read_u32_paused_locked(0x80825840) or 0
            self._write_virtual_memory_paused_locked(scratch_va, bytes(0x1C))
            poll_call = self._call_guest_function_stepped_paused_locked(
                0x800DC588,
                args=(scratch_va, 0, 0, 0),
                return_pc=0x80008A8C,
                max_steps=4096,
                max_recorded_steps=16,
            )
            record = self._read_virtual_memory_paused_locked(scratch_va, 0x1C)
            words = [struct.unpack_from("<I", record, offset)[0] for offset in range(0, 0x1C, 4)]
            row: dict[str, object] = {
                "index": index,
                "flags_before": f"0x{flags_before:08x}",
                "poll_call": poll_call,
                "record_words": [f"0x{word:08x}" for word in words],
                "object": f"0x{words[0]:08x}",
                "gui_event": f"0x{words[1]:08x}",
            }
            if words[1] != 0:
                dispatch_call = self._call_guest_function_stepped_paused_locked(
                    0x800D3D04,
                    args=(words[0], words[1], words[2], words[3]),
                    return_pc=0x80008A8C,
                    max_steps=4096,
                    max_recorded_steps=16,
                    continue_timeout=2.0,
                )
                row["dispatch_call"] = dispatch_call
            flags_after = self._read_u32_paused_locked(0x80825840) or 0
            row["flags_after"] = f"0x{flags_after:08x}"
            events.append(row)
            if flags_after == 0 or words[1] == 0:
                break
            if not poll_call.get("returned"):
                row["warning"] = poll_call.get("error") or "poller did not return"
                break
        flags_after_all = self._read_u32_paused_locked(0x80825840) or 0
        out["events"] = events
        out["flags_after"] = f"0x{flags_after_all:08x}"
        out["drained"] = flags_after_all == 0
        return out

    def _settle_gui_repaint_paused_locked(self, *, rounds: int = 3, delay: float = 0.08) -> dict[str, object]:
        """Give guest-side repaint work a chance to run after synthesized GUI events."""

        if self.config.machine.lower() == "bbk9588":
            row = self._bbk9588_python_guest_service_disabled("qemu-gui-repaint-settle")
            row.update({"rounds": [], "settled": False})
            return row
        out: dict[str, object] = {
            "event": "qemu-gui-repaint-settle",
            "rounds": [],
        }
        if self.proc is None or self.proc.poll() is not None:
            out["error"] = "QEMU process is not running"
            return out
        if self.gdb_sock is None:
            out["error"] = "QEMU GDB stub is not connected"
            return out
        for index in range(max(0, int(rounds))):
            row: dict[str, object] = {"index": index}
            try:
                before_pc = self._read_pc_paused_locked()
                before_flags = self._read_u32_paused_locked(0x80825840) or 0
                row["pc_before"] = self._format_u32(before_pc)
                row["flags_before"] = f"0x{before_flags:08x}"
                self._resume_after_gdb_locked()
                time.sleep(max(0.0, float(delay)))
                self._pause_for_gdb_locked()
                after_pc = self._read_pc_paused_locked()
                after_flags = self._read_u32_paused_locked(0x80825840) or 0
                row["pc_after_run"] = self._format_u32(after_pc)
                row["flags_after_run"] = f"0x{after_flags:08x}"
                if after_flags:
                    row["event_poller"] = self._pump_gui_event_poller_paused_locked()
                row["idle_pump"] = self._pump_gui_idle_dispatcher_paused_locked()
                row["pc_after_pump"] = self._format_u32(self._read_pc_paused_locked())
                row["flags_after_pump"] = f"0x{(self._read_u32_paused_locked(0x80825840) or 0):08x}"
                out["rounds"].append(row)
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                out["rounds"].append(row)
                out["error"] = row["error"]
                break
        out["final_flags"] = f"0x{(self._read_u32_paused_locked(0x80825840) or 0):08x}"
        out["final_pc"] = self._format_u32(self._read_pc_paused_locked())
        out["settled"] = out["final_flags"] == "0x00000000"
        return out

    def _snapshot_locked(self) -> dict[str, object]:
        if self.proc is not None:
            self._record_process_exit_locked(self.proc.poll())
        now = time.time()
        elapsed = None if self.started_at is None else (self.finished_at or now) - self.started_at
        performance = self._update_performance_metrics_locked(now, elapsed)
        register_sample = self.register_sample
        pc_classification = (
            classify_guest_pc(register_sample.get("pc"))
            if isinstance(register_sample, dict)
            else None
        )
        snapshot = {
                "command": list(self.command),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": None if elapsed is None else round(max(0.0, elapsed), 3),
                "running": self.proc is not None and self.proc.poll() is None,
                "process_id": None if self.proc is None else self.proc.pid,
                "returncode": self.returncode,
                "exit_reason": self.exit_reason,
                "stop_requested": self.stop_requested,
                "stdout_tail": list(self.stdout_tail[-80:]),
                "stderr_tail": list(self.stderr_tail[-80:]),
                "last_error": self.last_error,
                "nand_image": (
                    None if self.config.nand_image is None
                    else str(self.config.nand_image.resolve())
                ),
                "nand_write_mode": (
                    "direct" if self.config.nand_image is not None else "none"
                ),
                "kill_with_parent": self._process_job_handle is not None,
                "process_job_error": self.last_process_job_error,
                "hmp_port": self.hmp_port,
                "qmp_port": self.qmp_port,
                "qmp_last_event": self.qmp_last_event,
                "last_qmp_error": self.last_qmp_error,
                "gdb_port": self.gdb_port,
                "gdb_connected": self.gdb_sock is not None,
                "bbk_input_port": self.bbk_input_port,
                "bbk_input_connected": self.bbk_input_sock is not None,
                "bbk_input_write_count": self.bbk_input_write_count,
                "last_bbk_input_error": self.last_bbk_input_error,
                "usb_power_connected": self.usb_power_connected,
                "register_sample": register_sample,
                "pc": register_sample.get("pc") if isinstance(register_sample, dict) else None,
                "cp0": register_sample.get("cp0") if isinstance(register_sample, dict) else None,
                "pc_classification": pc_classification,
                "register_sample_at": self.register_sample_at,
                "memory_read_count": self.memory_read_count,
                "last_memory_read_error": self.last_memory_read_error,
                "display_screendump_count": self.display_screendump_count,
                "last_display_screendump_error": self.last_display_screendump_error,
                "bbk_frame_port": self.bbk_frame_port,
                "bbk_frame_connected": self.bbk_frame_sock is not None,
                "frame_chardev_count": self.frame_chardev_count,
                "last_frame_chardev_error": self.last_frame_chardev_error,
                "audio_stream_packet_count": self.audio_stream_packet_count,
                "audio_stream_bytes": self.audio_stream_bytes,
                "last_audio_stream_error": self.last_audio_stream_error,
                "last_guest_insn_error": self.last_guest_insn_error,
                "performance": performance,
                "gdb_read_count": self.gdb_read_count,
                "gdb_write_count": self.gdb_write_count,
                "gdb_register_read_count": self.gdb_register_read_count,
                "gdb_register_write_count": self.gdb_register_write_count,
                "gdb_step_count": self.gdb_step_count,
                "guest_call_count": self.guest_call_count,
                "legacy_python_storage_hook_count": self.legacy_python_storage_hook_count,
                "legacy_python_storage_hook_events": list(self.legacy_python_storage_hook_events[-16:]),
                "event_loop_empty_fix_count": self.event_loop_empty_fix_count,
                "event_loop_synth_event_count": self.event_loop_synth_event_count,
                "task_context_trace_count": self.task_context_trace_count,
                "task_context_events": list(self.task_context_events[-16:]),
                "gui_timer_tick_count": self.gui_timer_tick_count,
                "gui_timer_fire_count": self.gui_timer_fire_count,
                "gui_timer_events": list(self.gui_timer_events[-16:]),
                "fs_trace_count": self.fs_trace_count,
                "fs_trace_events": list(self.fs_trace_events[-16:]),
                "event_loop_trace_count": self.event_loop_trace_count,
                "event_loop_trace_events": list(self.event_loop_trace_events[-16:]),
                "resource_trace_count": self.resource_trace_count,
                "resource_trace_events": list(self.resource_trace_events[-16:]),
                "last_gdb_error": self.last_gdb_error,
                "guest_input_events": list(self.guest_input_events[-16:]),
        }
        self._last_snapshot = dict(snapshot)
        return snapshot

    def snapshot(self, *, refresh: bool = True) -> dict[str, object]:
        if refresh:
            self.refresh()
        if not refresh:
            acquired = self._lock.acquire(blocking=False)
            if not acquired:
                if self._last_snapshot:
                    return dict(self._last_snapshot)
                with self._lock:
                    return self._snapshot_locked()
            try:
                return self._snapshot_locked()
            finally:
                self._lock.release()
        with self._lock:
            return self._snapshot_locked()


def find_qemu(executable: str = DEFAULT_QEMU_EXECUTABLE) -> str | None:
    """Return an executable path if QEMU is available locally or on PATH."""

    path = Path(executable)
    if path.is_file():
        return str(path)
    names = [executable]
    if not executable.lower().endswith(".exe"):
        names.append(executable + ".exe")
    if executable == DEFAULT_QEMU_EXECUTABLE:
        names.append("bbk9588-qemu-system-mipsel.exe")
    package_root = Path(__file__).resolve().parents[2]
    roots = [
        package_root / "bin",
        Path("E:/qemu-src/build-bbk9588-win"),
        Path("E:/qemu-src/build"),
        Path("E:/qemu"),
        Path("C:/Program Files/qemu"),
        Path("C:/Program Files (x86)/qemu"),
        Path.home() / "AppData/Local/qemu",
    ]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return str(candidate)
    resolved = shutil.which(executable)
    if resolved is not None:
        return resolved
    return None


def qemu_subprocess_env(executable: str) -> dict[str, str]:
    env = os.environ.copy()
    path_entries: list[str] = []
    exe_path = Path(executable)
    exe_text = str(exe_path).replace("\\", "/").lower()
    if "qemu-src/build" in exe_text or "msys64" in exe_text:
        for candidate in (Path("C:/msys64/ucrt64/bin"), Path("C:/msys64/usr/bin")):
            if candidate.is_dir():
                path_entries.append(str(candidate))
    if path_entries:
        current = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([*path_entries, current]) if current else os.pathsep.join(path_entries)
    return env


def find_workspace_file(name: str) -> Path:
    matches = sorted(
        path
        for path in Path(".").rglob(name)
        if path.is_file() and not path.parts[:1] == ("build",)
    )
    if not matches:
        raise FileNotFoundError(f"could not find {name!r} under current workspace")
    return matches[0]


def find_default_qemu_nand_image() -> Path | None:
    if DEFAULT_QEMU_NAND_IMAGE.is_file():
        return DEFAULT_QEMU_NAND_IMAGE
    return None


def is_ascii_path(path: Path) -> bool:
    try:
        str(path.resolve()).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def qemu_safe_payload_path(path: Path) -> Path:
    source = path.resolve()
    if is_ascii_path(source):
        return source
    stage_dir = Path("build") / "qemu_payloads"
    stage_dir.mkdir(parents=True, exist_ok=True)
    target = stage_dir / source.name
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    return target.resolve()


def migrate_legacy_nand_checkpoint(path: Path) -> Path | None:
    """Replace an active NAND with the old checkpoint once, then remove it."""

    active = path.resolve()
    suffix = active.suffix or ".bin"
    digest = hashlib.sha1(
        os.path.normcase(str(active)).encode("utf-8")
    ).hexdigest()[:16]
    checkpoint = (
        Path("runtime") / "qemu_nand_persistent" / f"nand_{digest}{suffix}"
    ).resolve()
    if not checkpoint.is_file():
        return None

    active.parent.mkdir(parents=True, exist_ok=True)
    temporary = active.with_name(
        f".{active.name}.{os.getpid()}.{time.time_ns()}.migrate.tmp"
    )
    try:
        shutil.copy2(checkpoint, temporary)
        normalize_c200_logical_tail_pages(temporary)
        os.replace(temporary, active)
        checkpoint.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return checkpoint

def qemu_patched_payload_path(path: Path, *, base: int, patch_names: tuple[str, ...]) -> Path:
    source = qemu_safe_payload_path(path)
    if not patch_names:
        return source
    unknown = [name for name in patch_names if name not in KNOWN_FIRMWARE_PATCHES]
    if unknown:
        raise ValueError(f"unknown QEMU firmware patch(es): {', '.join(unknown)}")
    digest = hashlib.sha1(source.read_bytes()).hexdigest()[:12]
    patch_digest = hashlib.sha1("\n".join(patch_names).encode("utf-8")).hexdigest()[:12]
    target = (Path("build") / "qemu_payloads" / f"{source.stem}_{digest}_patches_{patch_digest}{source.suffix}").resolve()
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    data = bytearray(source.read_bytes())
    for name in patch_names:
        for va, patch in KNOWN_FIRMWARE_PATCHES[name]:
            offset = va - base
            if offset < 0 or offset + len(patch) > len(data):
                raise ValueError(f"patch {name} address 0x{va:08x} is outside {source}")
            data[offset : offset + len(patch)] = patch
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def default_firmware_patches_for_machine(machine: str) -> tuple[str, ...]:
    if machine.lower() == "bbk9588":
        return DEFAULT_BBK9588_FIRMWARE_PATCHES
    return DEFAULT_QEMU_FIRMWARE_PATCHES


def normalize_firmware_patches(
    values: tuple[str, ...] | list[str] | None,
    *,
    default: tuple[str, ...] = DEFAULT_QEMU_FIRMWARE_PATCHES,
) -> tuple[str, ...]:
    if values is None:
        return default
    cleaned = tuple(str(value) for value in values if str(value))
    if any(value.lower() == "none" for value in cleaned):
        return ()
    return cleaned


def default_bbk9588_machine_options(values: tuple[str, ...]) -> tuple[str, ...]:
    """Return launcher compatibility options not explicitly overridden by user."""

    options = tuple(str(value) for value in values if str(value))
    removed = [
        option
        for option in options
        if option.split("=", 1)[0] in REMOVED_BBK9588_MACHINE_OPTIONS
    ]
    if removed:
        joined = ", ".join(removed)
        raise ValueError(
            f"removed bbk9588 machine option(s): {joined}; "
            "the default path now uses modeled QEMU devices instead of legacy fastpath switches"
        )
    return (*DEFAULT_BBK9588_COMPAT_MACHINE_OPTIONS, *options)


def classify_guest_pc(pc: int | str | None) -> dict[str, object] | None:
    if pc is None:
        return None
    value = int(str(pc), 0) if isinstance(pc, str) else int(pc)
    value &= 0xFFFFFFFF
    for start, end, name, description in KNOWN_STALL_REGIONS:
        if start <= value <= end:
            return {
                "pc": f"0x{value:08x}",
                "region": name,
                "range": [f"0x{start:08x}", f"0x{end:08x}"],
                "description": description,
            }
    return {"pc": f"0x{value:08x}", "region": "unknown", "range": None, "description": ""}


def build_bbk_qemu_config(
    *,
    boot_mode: str = "nand",
    executable: str = DEFAULT_QEMU_EXECUTABLE,
    image: Path | None = None,
    payload: Path | None = None,
    payload_addr: int = DEFAULT_C200_PHYS,
    nand_image: Path | None = None,
    load_addr: int | None = None,
    pc: int | None = None,
    machine: str = DEFAULT_QEMU_MACHINE,
    cpu: str = DEFAULT_QEMU_CPU,
    ram_mb: int = 160,
    accel: str = "tcg,thread=multi,tb-size=256",
    display: str = "none",
    serial: str = "mon:stdio",
    monitor: str = "none",
    gdb: str = "none",
    bbk_input: str = "none",
    bbk_frame: str = "none",
    bbk_machine_options: tuple[str, ...] = (),
    timeout_seconds: float = 5.0,
    plugins: tuple[Path, ...] = (),
    extra_args: tuple[str, ...] = (),
    firmware_patches: tuple[str, ...] | list[str] | None = None,
    hibernate_wakeup: bool = False,
    startup_power_key: bool = False,
    startup_power_hold_seconds: float = 0.75,
) -> QemuSystemConfig:
    firmware_patches = normalize_firmware_patches(
        firmware_patches,
        default=default_firmware_patches_for_machine(machine),
    )
    if boot_mode == "nand":
        if machine.lower() != "bbk9588":
            raise ValueError("boot_mode='nand' requires the bbk9588 machine")
        if image is not None or payload is not None:
            raise ValueError("boot_mode='nand' starts from the NAND first-stage loader; use boot_mode='c200' or 'uboot' for --image/--payload")
        boot_load_addr = load_addr if load_addr is not None else DEFAULT_BOOTROM_LOAD_PHYS
        boot_payload = None
        extra_payloads = ()
        boot_pc = pc if pc is not None else DEFAULT_BOOTROM_ENTRY
        bbk_machine_options = (
            "bootrom-nand=on",
            *bbk_machine_options,
        )
    elif boot_mode == "uboot":
        if image is None and machine.lower() == "bbk9588":
            boot_load_addr = load_addr if load_addr is not None else DEFAULT_BOOTROM_LOAD_PHYS
            boot_payload = None
            bbk_machine_options = (
                "bootrom-nand=on",
                *bbk_machine_options,
            )
            boot_pc = pc if pc is not None else DEFAULT_BOOTROM_ENTRY
        else:
            boot_load_addr = load_addr if load_addr is not None else DEFAULT_UBOOT_PHYS
            boot_image = qemu_safe_payload_path(image or find_workspace_file("u_boot_9588_4740.bin"))
            boot_payload = QemuPayload(boot_image, boot_load_addr)
            boot_pc = pc if pc is not None else DEFAULT_UBOOT_BASE
        if payload is not None:
            c200 = qemu_patched_payload_path(
                payload,
                base=DEFAULT_C200_BASE,
                patch_names=firmware_patches,
            )
            extra_payloads = (QemuPayload(c200, payload_addr),)
        else:
            extra_payloads = ()
    elif boot_mode == "c200":
        boot_image = qemu_patched_payload_path(
            image or find_workspace_file("C200.bin"),
            base=DEFAULT_C200_BASE,
            patch_names=firmware_patches,
        )
        boot_load_addr = load_addr if load_addr is not None else DEFAULT_C200_PHYS
        boot_payload = QemuPayload(boot_image, boot_load_addr)
        extra_payloads = ()
        boot_pc = pc if pc is not None else DEFAULT_C200_BASE
    else:
        raise ValueError(f"unsupported QEMU boot_mode {boot_mode!r}")
    if machine.lower() == "bbk9588":
        option_names = {
            option.split("=", 1)[0] for option in bbk_machine_options
        }
        if hibernate_wakeup and "hibernate-wakeup" not in option_names:
            bbk_machine_options = (*bbk_machine_options, "hibernate-wakeup=on")
        bbk_machine_options = default_bbk9588_machine_options(bbk_machine_options)
    return QemuSystemConfig(
        executable=executable,
        machine=machine,
        cpu=cpu,
        ram_mb=ram_mb,
        accel=accel,
        display=display,
        serial=serial,
        monitor=monitor,
        gdb=gdb,
        bbk_input=bbk_input,
        bbk_frame=bbk_frame,
        bbk_machine_options=bbk_machine_options,
        boot_payload=boot_payload,
        boot_load_addr=boot_load_addr,
        boot_pc=boot_pc,
        nand_image=None if nand_image is None else nand_image.resolve(),
        extra_payloads=extra_payloads,
        plugins=tuple(Path(plugin) for plugin in plugins),
        extra_args=extra_args,
        timeout_seconds=timeout_seconds,
        firmware_patches=firmware_patches,
        startup_power_key=bool(startup_power_key),
        startup_power_hold_seconds=max(0.0, float(startup_power_hold_seconds)),
    )


def build_qemu_command(config: QemuSystemConfig) -> list[str]:
    """Build a qemu-system-mipsel command for a raw BBK firmware image."""

    if config.boot_payload is None and config.machine.lower() != "bbk9588":
        raise ValueError("boot_payload is required")
    if config.ram_mb <= 0:
        raise ValueError("ram_mb must be positive")
    machine_arg = config.machine
    bbk_machine_options = list(config.bbk_machine_options)
    if config.machine.lower() == "bbk9588":
        option_names = {option.split("=", 1)[0] for option in bbk_machine_options}
        if config.boot_payload is None and "bootrom-nand" not in option_names:
            raise ValueError("bbk9588 requires boot_payload or bootrom-nand=on")
        if "firmware-phys" not in option_names:
            bbk_machine_options.append(f"firmware-phys=0x{config.boot_load_addr:x}")
        if "reset-pc" not in option_names:
            bbk_machine_options.append(f"reset-pc=0x{config.boot_pc:x}")
    if config.machine.lower() == "bbk9588" and bbk_machine_options:
        machine_arg = ",".join((machine_arg, *bbk_machine_options))
    if config.machine.lower() == "bbk9588" and config.bbk_input != "none":
        machine_arg = f"{machine_arg},input-chardev={DEFAULT_QEMU_BBK_INPUT_CHR_ID}"
    if config.machine.lower() == "bbk9588" and config.bbk_frame != "none":
        machine_arg = f"{machine_arg},frame-chardev={DEFAULT_QEMU_BBK_FRAME_CHR_ID}"
    command = [
        config.executable,
        "-M",
        machine_arg,
        "-cpu",
        config.cpu,
        "-m",
        str(config.ram_mb),
        "-accel",
        config.accel,
        "-display",
        config.display,
        "-serial",
        config.serial,
        "-monitor",
        config.monitor,
    ]
    if config.gdb != "none":
        command.extend(["-gdb", config.gdb])
    if config.machine.lower() == "bbk9588" and config.bbk_input != "none":
        command.extend(["-chardev", config.bbk_input])
    if config.machine.lower() == "bbk9588" and config.bbk_frame != "none":
        command.extend(["-chardev", config.bbk_frame])
    if config.machine.lower() == "bbk9588" and config.nand_image is not None:
        nand_path = str(config.nand_image.resolve()).replace("\\", "/")
        command.extend([
            "-drive",
            f"if=mtd,index=0,format=raw,cache=writeback,file={nand_path}",
        ])
    for plugin in config.plugins:
        plugin_path = str(plugin.resolve()).replace("\\", "/")
        command.extend(["-plugin", f"file={plugin_path}"])
    command.append("-no-reboot")
    if config.machine.lower() == "bbk9588":
        if config.boot_payload is not None:
            command.extend(["-kernel", str(config.boot_payload.path.resolve())])
    else:
        if config.boot_payload is None:
            raise ValueError("boot_payload is required")
        command.extend(
            [
                "-device",
                config.boot_payload.loader_arg(set_pc=False),
                "-device",
                f"loader,addr=0x{config.boot_pc:x},cpu-num=0",
            ]
        )
    for payload in config.extra_payloads:
        command.extend(["-device", payload.loader_arg(set_pc=False)])
    command.extend(config.extra_args)
    return command


def _find_free_tcp_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _process_cpu_time_seconds(pid: int) -> float | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            kernel_ticks = (int(kernel.dwHighDateTime) << 32) | int(kernel.dwLowDateTime)
            user_ticks = (int(user.dwHighDateTime) << 32) | int(user.dwLowDateTime)
            return (kernel_ticks + user_ticks) / 10_000_000.0
        finally:
            kernel32.CloseHandle(handle)

    stat = Path(f"/proc/{pid}/stat")
    try:
        text = stat.read_text(encoding="ascii")
        fields = text.rsplit(")", 1)[1].strip().split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[11]) + int(fields[12])) / float(ticks)
    except Exception:
        return None


def _read_hmp_available(sock: socket.socket, timeout: float = 0.2) -> str:
    chunks: list[bytes] = []
    sock.settimeout(timeout)
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if len(chunk) < 65536:
            break
    return b"".join(chunks).decode("utf-8", "replace")


def _connect_hmp(port: int, timeout: float = 3.0) -> socket.socket:
    deadline = time.time() + timeout
    last_error: OSError | None = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            _read_hmp_available(sock, 0.2)
            return sock
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to QEMU HMP monitor on port {port}: {last_error}")


def _hmp_command(sock: socket.socket, command: str) -> str:
    sock.sendall(command.encode("ascii") + b"\n")
    time.sleep(0.05)
    text = _read_hmp_available(sock, 0.5)
    if "(qemu)" not in text:
        text += _read_hmp_available(sock, 0.5)
    return text


def _read_qmp_message(sock: socket.socket) -> dict[str, object]:
    line = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            raise EOFError("QEMU QMP monitor closed")
        if byte == b"\n":
            if not line.strip():
                line.clear()
                continue
            message = json.loads(line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("invalid QMP message")
            return message
        line.extend(byte)


def _connect_qmp(port: int, timeout: float = 3.0) -> socket.socket:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            sock.settimeout(max(0.25, deadline - time.time()))
            greeting = _read_qmp_message(sock)
            if "QMP" not in greeting:
                raise ValueError("missing QMP greeting")
            sock.sendall(b'{"execute":"qmp_capabilities"}\r\n')
            while True:
                response = _read_qmp_message(sock)
                if "return" in response:
                    sock.settimeout(None)
                    return sock
                if "error" in response:
                    raise RuntimeError(f"QMP capability negotiation failed: {response['error']}")
        except (OSError, EOFError, ValueError, RuntimeError) as exc:
            last_error = exc
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to QEMU QMP monitor on port {port}: {last_error}")


def _connect_gdb(port: int, timeout: float = 3.0) -> socket.socket:
    deadline = time.time() + timeout
    last_error: OSError | None = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            sock.settimeout(1.0)
            return sock
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to QEMU GDB stub on port {port}: {last_error}")


def _connect_bbk_input(port: int, timeout: float = 3.0) -> socket.socket:
    deadline = time.time() + timeout
    last_error: OSError | None = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            sock.settimeout(1.0)
            return sock
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to QEMU BBK input port {port}: {last_error}")


def _ppm_next_token(data: bytes, offset: int) -> tuple[bytes, int]:
    size = len(data)
    while offset < size:
        ch = data[offset]
        if ch in b" \t\r\n":
            offset += 1
            continue
        if ch == ord("#"):
            while offset < size and data[offset] not in b"\r\n":
                offset += 1
            continue
        break
    start = offset
    while offset < size and data[offset] not in b" \t\r\n":
        offset += 1
    if start == offset:
        raise ValueError("truncated PPM header")
    return data[start:offset], offset


def _ppm_to_rgb565(data: bytes) -> bytes:
    token, offset = _ppm_next_token(data, 0)
    if token != b"P6":
        raise ValueError("unsupported PPM magic")
    width_b, offset = _ppm_next_token(data, offset)
    height_b, offset = _ppm_next_token(data, offset)
    maxval_b, offset = _ppm_next_token(data, offset)
    width = int(width_b)
    height = int(height_b)
    maxval = int(maxval_b)
    if width != 240 or height != 320 or maxval != 255:
        raise ValueError(f"unexpected PPM geometry {width}x{height} max={maxval}")
    if offset >= len(data) or data[offset] not in b" \t\r\n":
        raise ValueError("missing PPM raster separator")
    offset += 1
    rgb = data[offset:]
    expected = width * height * 3
    if len(rgb) < expected:
        raise ValueError(f"truncated PPM raster: {len(rgb)} < {expected}")
    out = bytearray(width * height * 2)
    pos = 0
    for i in range(0, expected, 3):
        r = rgb[i]
        g = rgb[i + 1]
        b = rgb[i + 2]
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[pos] = value & 0xFF
        out[pos + 1] = (value >> 8) & 0xFF
        pos += 2
    return bytes(out)


def _gdb_checksum(payload: str) -> int:
    return sum(payload.encode("ascii")) & 0xFF


def _gdb_read_packet(sock: socket.socket, timeout: float = 2.0) -> str:
    sock.settimeout(timeout)
    while True:
        start = sock.recv(1)
        if not start:
            raise EOFError("GDB connection closed")
        if start == b"$":
            break
    payload = bytearray()
    while True:
        char = sock.recv(1)
        if not char:
            raise EOFError("GDB connection closed")
        if char == b"#":
            checksum = sock.recv(2)
            if len(checksum) != 2:
                raise EOFError("GDB checksum was truncated")
            expected = int(checksum.decode("ascii"), 16)
            actual = sum(payload) & 0xFF
            sock.sendall(b"+" if actual == expected else b"-")
            if actual != expected:
                raise IOError(f"GDB checksum mismatch: expected 0x{expected:02x}, got 0x{actual:02x}")
            return payload.decode("ascii")
        payload.extend(char)


def _gdb_packet(sock: socket.socket, payload: str) -> str:
    packet = f"${payload}#{_gdb_checksum(payload):02x}".encode("ascii")
    sock.sendall(packet)
    ack = sock.recv(1)
    if ack == b"-":
        sock.sendall(packet)
        ack = sock.recv(1)
    if ack != b"+":
        raise IOError(f"GDB did not acknowledge packet {payload!r}: {ack!r}")
    return _gdb_read_packet(sock)


def _gdb_interrupt(sock: socket.socket) -> str:
    sock.sendall(b"\x03")
    return _gdb_read_packet(sock)


def _gdb_continue(sock: socket.socket) -> None:
    packet = f"$c#{_gdb_checksum('c'):02x}".encode("ascii")
    sock.sendall(packet)
    ack = sock.recv(1)
    if ack not in {b"+", b""}:
        raise IOError(f"GDB did not acknowledge continue: {ack!r}")


def _gdb_continue_wait(sock: socket.socket, timeout: float = 2.0) -> str:
    packet = f"$c#{_gdb_checksum('c'):02x}".encode("ascii")
    sock.sendall(packet)
    ack = sock.recv(1)
    if ack != b"+":
        raise IOError(f"GDB did not acknowledge continue: {ack!r}")
    return _gdb_read_packet(sock, timeout=timeout)


def _gdb_insert_breakpoint(sock: socket.socket, addr: int, kind: int = 4) -> str:
    reply = _gdb_packet(sock, f"Z1,{addr & 0xFFFFFFFF:x},{kind:x}")
    if reply in {"", "E22", "E01"}:
        reply = _gdb_packet(sock, f"Z0,{addr & 0xFFFFFFFF:x},{kind:x}")
    if reply != "OK":
        raise IOError(f"GDB could not insert breakpoint at 0x{addr & 0xFFFFFFFF:08x}: {reply!r}")
    return reply


def _gdb_remove_breakpoint(sock: socket.socket, addr: int, kind: int = 4) -> str:
    replies = [
        _gdb_packet(sock, f"z1,{addr & 0xFFFFFFFF:x},{kind:x}"),
        _gdb_packet(sock, f"z0,{addr & 0xFFFFFFFF:x},{kind:x}"),
    ]
    bad = [reply for reply in replies if reply not in {"OK", "", "E22", "E01"}]
    if bad:
        raise IOError(f"GDB could not remove breakpoint at 0x{addr & 0xFFFFFFFF:08x}: {bad!r}")
    return "OK" if "OK" in replies else ""


def _gdb_step(sock: socket.socket) -> str:
    packet = f"$s#{_gdb_checksum('s'):02x}".encode("ascii")
    sock.sendall(packet)
    ack = sock.recv(1)
    if ack != b"+":
        raise IOError(f"GDB did not acknowledge step: {ack!r}")
    return _gdb_read_packet(sock)


def _parse_register_sample(raw: str, sample_at_seconds: float) -> dict[str, object]:
    sample: dict[str, object] = {
        "sample_at_seconds": round(sample_at_seconds, 3),
        "raw": raw,
    }
    for key in ("pc", "ra", "sp", "gp"):
        match = re.search(rf"\b{key}\s+(?:0x)?([0-9a-fA-F]{{8,16}})|\b{key}=(?:0x)?([0-9a-fA-F]{{8,16}})", raw)
        if match:
            value = match.group(1) or match.group(2)
            sample[key] = f"0x{int(value, 16) & 0xFFFFFFFF:08x}"
    pc_match = re.search(r"\bpc=0x([0-9a-fA-F]{8,16})", raw)
    if pc_match:
        sample["pc"] = f"0x{int(pc_match.group(1), 16) & 0xFFFFFFFF:08x}"
    cp0_match = re.search(
        r"\bCP0\s+Status\s+0x([0-9a-fA-F]{8,16})\s+Cause\s+0x([0-9a-fA-F]{8,16})\s+EPC\s+0x([0-9a-fA-F]{8,16})",
        raw,
    )
    if cp0_match:
        status = int(cp0_match.group(1), 16) & 0xFFFFFFFF
        cause = int(cp0_match.group(2), 16) & 0xFFFFFFFF
        epc = int(cp0_match.group(3), 16) & 0xFFFFFFFF
        sample["cp0_status"] = f"0x{status:08x}"
        sample["cp0_cause"] = f"0x{cause:08x}"
        sample["cp0_epc"] = f"0x{epc:08x}"
        sample["cp0"] = decode_cp0(status=status, cause=cause, epc=epc)
    badvaddr_match = re.search(r"\bBadVAddr\s+0x([0-9a-fA-F]{8,16})", raw)
    if badvaddr_match:
        badvaddr = int(badvaddr_match.group(1), 16) & 0xFFFFFFFF
        sample["cp0_badvaddr"] = f"0x{badvaddr:08x}"
        if isinstance(sample.get("cp0"), dict):
            sample["cp0"]["badvaddr"] = f"0x{badvaddr:08x}"
    return sample


def decode_cp0(*, status: int, cause: int, epc: int) -> dict[str, object]:
    """Decode the CP0 fields QEMU exposes through HMP `info registers`."""

    status &= 0xFFFFFFFF
    cause &= 0xFFFFFFFF
    epc &= 0xFFFFFFFF
    exception_code = (cause >> 2) & 0x1F
    pending_mask = (cause >> 8) & 0xFF
    interrupt_mask = (status >> 8) & 0xFF
    cpu_interrupts_enabled = bool(status & 0x1) and not bool(status & 0x2) and not bool(status & 0x4)
    enabled_pending = pending_mask & interrupt_mask if cpu_interrupts_enabled else 0
    return {
        "status": f"0x{status:08x}",
        "cause": f"0x{cause:08x}",
        "epc": f"0x{epc:08x}",
        "ie": bool(status & 0x1),
        "exl": bool(status & 0x2),
        "erl": bool(status & 0x4),
        "ksu": (status >> 3) & 0x3,
        "interrupt_mask": f"0x{interrupt_mask:02x}",
        "pending_interrupts": f"0x{pending_mask:02x}",
        "cpu_interrupts_enabled": cpu_interrupts_enabled,
        "pending_enabled_interrupts": f"0x{enabled_pending:02x}",
        "exception_code": exception_code,
        "exception": MIPS_EXCEPTION_CODES.get(exception_code, f"unknown-{exception_code}"),
        "bd": bool(cause & 0x80000000),
    }


def run_qemu(config: QemuSystemConfig, *, hmp_sample_offsets: tuple[float, ...] = ()) -> QemuLaunchResult:
    """Run QEMU for a bounded interval and collect process output."""

    hmp_port: int | None = None
    if hmp_sample_offsets:
        hmp_port = _find_free_tcp_port()
        config = replace(config, monitor=f"tcp:127.0.0.1:{hmp_port},server,nowait", serial="none")
    raw_command = build_qemu_command(config)
    resolved = find_qemu(raw_command[0])
    if resolved is not None:
        raw_command[0] = resolved
    command = tuple(raw_command)
    started = time.perf_counter()
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=qemu_subprocess_env(command[0]),
    )
    timed_out = False
    samples: list[dict[str, object]] = []
    hmp_sock: socket.socket | None = None
    try:
        if hmp_port is not None:
            try:
                hmp_sock = _connect_hmp(hmp_port)
                for offset in hmp_sample_offsets:
                    target = started + max(0.0, float(offset))
                    while time.perf_counter() < target:
                        if proc.poll() is not None:
                            break
                        time.sleep(min(0.05, target - time.perf_counter()))
                    if proc.poll() is not None:
                        break
                    raw = _hmp_command(hmp_sock, "info registers")
                    samples.append(_parse_register_sample(raw, time.perf_counter() - started))
            except Exception as exc:
                samples.append({"error": f"{type(exc).__name__}: {exc}"})
        remaining_timeout = max(0.1, config.timeout_seconds - (time.perf_counter() - started))
        stdout, stderr = proc.communicate(timeout=remaining_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        stdout, stderr = proc.communicate()
    finally:
        if hmp_sock is not None:
            try:
                hmp_sock.close()
            except OSError:
                pass
    elapsed = time.perf_counter() - started
    return QemuLaunchResult(
        command=command,
        returncode=proc.returncode,
        timed_out=timed_out,
        elapsed_seconds=elapsed,
        stdout=stdout,
        stderr=stderr,
        hmp_samples=tuple(samples),
    )


def result_json(result: QemuLaunchResult) -> str:
    return json.dumps(result.to_json_dict(), indent=2, ensure_ascii=False)
