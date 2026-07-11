#!/usr/bin/env python3
"""Copy the bundled BBK9588 QEMU source overlay into a QEMU checkout."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_overlay() -> Path:
    return repo_root() / "qemu" / "overlay"


def iter_overlay_files(overlay: Path) -> list[Path]:
    return sorted(
        path
        for path in overlay.rglob("*")
        if path.is_file()
        and path.name != "README.md"
        and path.suffix not in {".pyc", ".pyo"}
        and "__pycache__" not in path.parts
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu-source", type=Path, default=Path(r"E:\qemu-src"))
    ap.add_argument("--overlay", type=Path, default=default_overlay())
    ap.add_argument("--check", action="store_true", help="Check whether files already match.")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without copying files.")
    ns = ap.parse_args(argv)

    source = ns.qemu_source.resolve()
    overlay = ns.overlay.resolve()
    if not (source / "configure").is_file() or not (source / "meson.build").is_file():
        print(f"not a QEMU source tree: {source}", file=sys.stderr)
        return 2
    if not overlay.is_dir():
        print(f"overlay not found: {overlay}", file=sys.stderr)
        return 2

    files = iter_overlay_files(overlay)
    if not files:
        print(f"overlay has no files: {overlay}", file=sys.stderr)
        return 2

    mismatches = 0
    for src in files:
        rel = src.relative_to(overlay)
        dst = source / rel
        same = dst.is_file() and filecmp.cmp(src, dst, shallow=False)
        if ns.check:
            status = "ok" if same else "diff"
            print(f"{status} {rel.as_posix()}")
            if not same:
                mismatches += 1
            continue
        print(f"copy {rel.as_posix()}")
        if not ns.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            dst.touch()

    if ns.check and mismatches:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
