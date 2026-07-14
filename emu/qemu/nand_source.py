"""Validated atomic import of a raw BBK9588 NAND image or release archive."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import BinaryIO
from zipfile import ZipFile

from .nand_fs import validate_nand_image

MAX_NAND_IMAGE_BYTES = 600 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


def _copy_exact(source: BinaryIO, destination: Path, expected_size: int) -> str:
    if expected_size <= 0 or expected_size > MAX_NAND_IMAGE_BYTES:
        raise ValueError(f"unsupported NAND image size: {expected_size} bytes")
    digest = hashlib.sha256()
    received = 0
    with destination.open("wb") as output:
        while received < expected_size:
            chunk = source.read(min(COPY_CHUNK_BYTES, expected_size - received))
            if not chunk:
                raise EOFError(
                    f"short NAND image: expected {expected_size} bytes, received {received}"
                )
            output.write(chunk)
            digest.update(chunk)
            received += len(chunk)
        if source.read(1):
            raise ValueError("NAND image is larger than its declared size")
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def import_nand_source(source_path: Path, destination_path: Path) -> dict[str, object]:
    """Import one validated NAND under a caller-held destination lease."""

    source = source_path.resolve()
    destination = destination_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"NAND source does not exist: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if source == destination:
        validation = validate_nand_image(destination)
        return {
            "source": str(source),
            "destination": str(destination),
            "sha256": _file_sha256(destination),
            **validation,
        }

    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.importing"
    )
    try:
        if source.suffix.casefold() == ".zip":
            with ZipFile(source) as archive:
                images = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir() and Path(item.filename).suffix.casefold() == ".bin"
                ]
                if len(images) != 1:
                    raise ValueError(
                        "NAND archive must contain exactly one .bin file; "
                        f"found {len(images)}"
                    )
                image = images[0]
                if image.flag_bits & 0x1:
                    raise ValueError("encrypted NAND archives are not supported")
                with archive.open(image, "r") as input_stream:
                    source_digest = _copy_exact(
                        input_stream,
                        temporary,
                        image.file_size,
                    )
        else:
            with source.open("rb") as input_stream:
                source_digest = _copy_exact(
                    input_stream,
                    temporary,
                    source.stat().st_size,
                )

        target_digest = _file_sha256(temporary)
        if target_digest != source_digest:
            raise ValueError("copied NAND image checksum mismatch")
        validation = validate_nand_image(temporary)
        os.replace(temporary, destination)
        _fsync_parent_directory(destination.parent)
        legacy = destination.parent / "qemu_nand_persistent"
        if legacy.is_dir():
            shutil.rmtree(legacy)
        return {
            "source": str(source),
            "destination": str(destination),
            "sha256": target_digest,
            **validation,
        }
    finally:
        temporary.unlink(missing_ok=True)
