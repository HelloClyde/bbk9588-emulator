#!/usr/bin/env python3
"""Validate the structure of a BBK 9588 emulator release archive."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REQUIRED_COMMON = (
    "README.md",
    "PROJECT_README.md",
    "DATA_NOTICE.md",
    "start-web.cmd",
    "start-web.ps1",
    "emu/__init__.py",
    "emu/app.py",
    "emu/qemu/system.py",
    "tools/build_runtime_images.ps1",
    "tools/make_combined_nand.py",
    "tools/make_fat16_image.py",
    "tools/stamp_ftl_oob.py",
    "emu/web/frontend.py",
    "emu/web/frontend_server.py",
    "emu/web/frontend_state.py",
    "emu/web/frontend_ws.py",
)

REQUIRED_RUNTIME = (
    "bin/bbk9588-qemu-system-mipsel.exe",
)

FORBIDDEN_RUNTIME_PREFIXES = (
    "packaging/",
    "qemu/scripts/",
    "qemu/overlay/",
    "tests/",
)

FORBIDDEN_RUNTIME_FILES = (
    "emu/qemu/check_source_tree.py",
    "qemu/README.md",
    "tools/collect_qemu_runtime.ps1",
    "tools/package_emulator.ps1",
)

FORBIDDEN_DATA_SUFFIXES = (
    ".a",
    ".bda",
    ".bin",
    ".dba",
    ".dll",
    ".dlx",
    ".dylib",
    ".elf",
    ".exe",
    ".lib",
    ".map",
    ".o",
    ".obj",
    ".pdb",
)

ALLOWED_RUNTIME_BINARY_PREFIXES = (
    "bin/",
    "python/",
)


def _strip_archive_root(names: list[str]) -> list[str]:
    parts = [name.split("/", 1)[0] for name in names if name and "/" in name]
    if not parts:
        return names
    root = parts[0]
    if all(part == root for part in parts):
        prefix = root + "/"
        return [name[len(prefix) :] if name.startswith(prefix) else name for name in names]
    return names


def _is_binary_allowed(name: str) -> bool:
    lower = name.lower()
    return lower == "copying.lib" or lower.startswith(ALLOWED_RUNTIME_BINARY_PREFIXES)


def validate_archive(path: Path, *, runtime: bool) -> list[str]:
    issues: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [entry.filename.replace("\\", "/") for entry in archive.infolist() if not entry.is_dir()]
    normalized = _strip_archive_root(names)
    entries = set(normalized)

    required = list(REQUIRED_COMMON)
    if runtime:
        required.extend(REQUIRED_RUNTIME)
    for name in required:
        if name not in entries:
            issues.append(f"missing required entry: {name}")

    for name in sorted(entries):
        lower = name.lower()
        if any(lower.startswith(prefix) for prefix in FORBIDDEN_RUNTIME_PREFIXES):
            issues.append(f"runtime package contains development-only path: {name}")
        if lower in FORBIDDEN_RUNTIME_FILES:
            issues.append(f"runtime package contains development-only file: {name}")
        if lower.endswith(FORBIDDEN_DATA_SUFFIXES) and not _is_binary_allowed(name):
            issues.append(f"package contains forbidden firmware/build artifact: {name}")
        if lower == "bin/qemu-system-mipsel.exe":
            issues.append("runtime QEMU executable must use the bbk9588-branded name")

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--runtime", action="store_true", help="Validate the downloadable runtime package profile.")
    args = parser.parse_args(argv)

    issues = validate_archive(args.archive, runtime=args.runtime)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(f"release package ok: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
