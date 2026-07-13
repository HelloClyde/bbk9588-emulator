#!/usr/bin/env python3
"""Run BBK 9588 raw firmware under qemu-system-mipsel."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emu.qemu.system import (
    DEFAULT_C200_PHYS,
    DEFAULT_QEMU_EXECUTABLE,
    DEFAULT_QEMU_MACHINE,
    build_bbk_qemu_config,
    build_qemu_command,
    find_default_qemu_nand_image,
    find_qemu,
    qemu_subprocess_env,
    result_json,
    run_qemu,
)


def _boot_config(ns: argparse.Namespace):
    return build_bbk_qemu_config(
        boot_mode=ns.boot_mode,
        executable=ns.qemu,
        image=ns.image,
        payload=ns.payload,
        payload_addr=ns.payload_addr,
        nand_image=ns.nand_image or find_default_qemu_nand_image(),
        load_addr=ns.load_addr,
        pc=ns.pc,
        machine=ns.machine,
        cpu=ns.cpu,
        ram_mb=ns.ram_mb,
        accel=ns.accel,
        display=ns.display,
        serial=ns.serial,
        monitor=ns.monitor,
        bbk_machine_options=tuple(ns.qemu_machine_option or ()),
        extra_args=tuple(ns.extra_arg or ()),
        timeout_seconds=ns.timeout,
        firmware_patches=ns.qemu_firmware_patch,
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Launch BBK 9588 firmware with QEMU system emulation.")
    ap.add_argument("--qemu", default=DEFAULT_QEMU_EXECUTABLE)
    ap.add_argument("--boot-mode", choices=["nand", "c200", "uboot"], default="nand")
    ap.add_argument(
        "--image",
        type=Path,
        help="Optional direct boot image path for c200/uboot compatibility modes.",
    )
    ap.add_argument(
        "--payload",
        type=Path,
        help="For --boot-mode uboot: optional legacy RAM preload for a C200 payload; omitted by default.",
    )
    ap.add_argument("--nand-image", type=Path, help="Raw NAND image used by nand/uboot boot modes.")
    ap.add_argument("--payload-addr", type=lambda value: int(value, 0), default=DEFAULT_C200_PHYS)
    ap.add_argument("--load-addr", type=lambda value: int(value, 0), help="Physical load address for the boot image.")
    ap.add_argument("--pc", type=lambda value: int(value, 0), help="Initial virtual PC.")
    ap.add_argument("--machine", default=DEFAULT_QEMU_MACHINE)
    ap.add_argument("--cpu", default="24Kf")
    ap.add_argument("--ram-mb", type=int, default=160)
    ap.add_argument("--accel", default="tcg,thread=multi,tb-size=256")
    ap.add_argument("--display", default="none")
    ap.add_argument("--serial", default="mon:stdio")
    ap.add_argument("--monitor", default="none")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument(
        "--qemu-machine-option",
        action="append",
        help="Append one diagnostic bbk9588 -M option, for example progress-trace=on. Repeat as needed.",
    )
    ap.add_argument("--extra-arg", action="append", help="Append one raw QEMU argument. Repeat as needed.")
    ap.add_argument(
        "--qemu-firmware-patch",
        action="append",
        default=None,
        help="Legacy diagnostic QEMU-only firmware patch name for compatibility runs, or 'none'.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print the QEMU command as JSON without starting QEMU.")
    ap.add_argument("--probe", action="store_true", help="Check qemu-system-mipsel availability and print version text.")
    ns = ap.parse_args(argv)

    if ns.probe:
        resolved = find_qemu(ns.qemu)
        if resolved is None:
            print(json.dumps({"ok": False, "qemu": ns.qemu, "error": "not found on PATH"}, ensure_ascii=False))
            return 1
        completed = subprocess.run(
            [resolved, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=qemu_subprocess_env(resolved),
        )
        print(
            json.dumps(
                {
                    "ok": completed.returncode == 0,
                    "qemu": resolved,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
                ensure_ascii=False,
            )
        )
        return 0 if completed.returncode == 0 else 1

    config = _boot_config(ns)
    command = build_qemu_command(config)
    if ns.dry_run:
        print(json.dumps({"command": command}, indent=2, ensure_ascii=False))
        return 0
    if find_qemu(ns.qemu) is None:
        print(json.dumps({"ok": False, "qemu": ns.qemu, "error": "not found on PATH"}, ensure_ascii=False))
        return 1
    result = run_qemu(config)
    print(result_json(result))
    return 0 if result.returncode == 0 or result.timed_out else 1


if __name__ == "__main__":
    raise SystemExit(main())
