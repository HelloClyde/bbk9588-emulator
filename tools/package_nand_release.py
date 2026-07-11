#!/usr/bin/env python3
"""Package an authorized BBK 9588 NAND image as a GitHub Release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

SUPPORTED_NAND_SIZES = {536_870_912, 553_648_128}
CHUNK_SIZE = 4 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def package_nand(source: Path, output_dir: Path, version: str) -> dict[str, object]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    size = source.stat().st_size
    if size not in SUPPORTED_NAND_SIZES:
        raise ValueError(f"unsupported NAND size {size} bytes")

    safe_version = "".join(character if character.isalnum() or character in ".-_" else "-" for character in version)
    safe_version = safe_version.strip("-") or "local"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"bbk9588_nand-{safe_version}.zip"
    manifest_path = output_dir / f"bbk9588_nand-{safe_version}.manifest.json"
    checksum_path = archive.with_suffix(archive.suffix + ".sha256")

    raw_sha256 = sha256_file(source)
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as bundle:
        bundle.write(source, "bbk9588_nand.bin")
    archive_sha256 = sha256_file(archive)

    manifest: dict[str, object] = {
        "format": "bbk9588-raw-nand-release-v1",
        "version": safe_version,
        "raw_name": "bbk9588_nand.bin",
        "raw_size": size,
        "raw_sha256": raw_sha256,
        "archive_name": archive.name,
        "archive_size": archive.stat().st_size,
        "archive_sha256": archive_sha256,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksum_path.write_text(f"{archive_sha256}  {archive.name}\n", encoding="ascii")
    return {
        **manifest,
        "archive": str(archive),
        "manifest": str(manifest_path),
        "checksum": str(checksum_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("build/nand-release"))
    parser.add_argument("--version", default="v1")
    args = parser.parse_args(argv)
    print(json.dumps(package_nand(args.source, args.output_dir, args.version), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
