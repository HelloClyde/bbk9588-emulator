#!/usr/bin/env python3
"""Validate a QEMU source checkout for the bundled BBK9588 overlay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_QEMU_SOURCE_PATHS = (
    "configure",
    "meson.build",
    "hw/mips/meson.build",
    "hw/mips/Kconfig",
    "target/mips",
)

OVERLAY_QEMU_SOURCE_PATHS = (
    "hw/audio/jz4740_aic.c",
    "hw/audio/meson.build",
    "hw/dma/jz4740_dmac.c",
    "hw/intc/jz4740_intc.c",
    "hw/misc/jz4740_cpm.c",
    "hw/mips/bbk9588.c",
    "hw/mips/Kconfig",
    "hw/mips/meson.build",
    "hw/timer/jz4740_tcu.c",
    "include/hw/audio/jz4740_aic.h",
    "include/hw/dma/jz4740_dmac.h",
    "include/hw/intc/jz4740_intc.h",
    "include/hw/misc/jz4740_cpm.h",
    "include/hw/timer/jz4740_tcu.h",
    "target/mips/tcg/op_helper.c",
    "target/mips/tcg/translate.c",
)


def inspect_qemu_source(root: Path) -> dict[str, object]:
    root = root.resolve()
    missing = [rel for rel in REQUIRED_QEMU_SOURCE_PATHS if not (root / rel).exists()]
    overlay_missing = [rel for rel in OVERLAY_QEMU_SOURCE_PATHS if not (root / rel).exists()]
    return {
        "root": str(root),
        "exists": root.exists(),
        "is_qemu_source": root.exists() and not missing,
        "missing_required_paths": missing,
        "bbk9588_overlay_looks_installed": root.exists() and not overlay_missing,
        "missing_overlay_paths": overlay_missing,
        "warning": None
        if root.name.lower() != "qemu"
        else "A directory named qemu may be a binary install. Verify it has configure, meson.build, and hw/mips before editing.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", default=r"E:\qemu-src", type=Path)
    ns = ap.parse_args(argv)

    result = inspect_qemu_source(ns.source)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["is_qemu_source"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
