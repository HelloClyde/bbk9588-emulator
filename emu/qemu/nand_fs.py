"""Host-side file management for the BBK9588 NAND logical FAT16 volume."""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
import threading
import zlib
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from pyfatfs.EightDotThree import EightDotThree
from pyfatfs.PyFatFS import PyFatFS

from .ecc import jz4740_page_oob_ecc
from .ftl import scan_ftl_image

PAGE_SIZE = 2048
SPARE_SIZE = 64
PAGE_STRIDE = PAGE_SIZE + SPARE_SIZE
PAGES_PER_BLOCK = 64
RAW_BLOCK_SIZE = PAGE_STRIDE * PAGES_PER_BLOCK
LOGICAL_BLOCK_SIZE = PAGE_SIZE * PAGES_PER_BLOCK
DEFAULT_VOLUME_LBA = 0x20
_SHORT_NAME_PATCH_LOCK = threading.RLock()


def _make_gbk_safe_8dot3_name(dir_name: str, parent_dir_entry: Any) -> str:
    """Use ASCII SFNs when a Unicode name cannot fit pyfatfs' GBK padding."""
    original = _ORIGINAL_MAKE_8DOT3_NAME
    try:
        dir_name.encode("ascii")
    except UnicodeEncodeError:
        pass
    else:
        return original(dir_name, parent_dir_entry)

    dirs, files, _special = parent_dir_entry.get_entries()
    existing = {entry.get_short_name().upper() for entry in dirs + files}
    stem, suffix = os.path.splitext(dir_name)
    extension = "".join(
        character
        for character in suffix[1:].upper()
        if character.isascii() and character.isalnum()
    )[:3]
    digest = f"{zlib.crc32(stem.encode('utf-8')) & 0xFFFF:04X}"
    for sequence in range(1, 1_000_000):
        tail = f"~{sequence}"
        basename = f"BB{digest}"[: 8 - len(tail)] + tail
        candidate = basename + (f".{extension}" if extension else "")
        if candidate.upper() not in existing:
            return candidate
    raise ValueError(f"unable to allocate FAT short name for {dir_name!r}")


_ORIGINAL_MAKE_8DOT3_NAME = EightDotThree.make_8dot3_name
_ORIGINAL_IS_8DOT3_CONFORM = EightDotThree.is_8dot3_conform


def _is_gbk_safe_8dot3_conform(entry_name: str, encoding: str = "ibm437") -> bool:
    try:
        entry_name.encode("ascii")
    except UnicodeEncodeError:
        return False
    return _ORIGINAL_IS_8DOT3_CONFORM(entry_name, encoding)


@contextmanager
def _gbk_safe_short_names():
    # pyfatfs pads 8.3 names by Unicode characters, which corrupts GBK SFNs.
    with _SHORT_NAME_PATCH_LOCK:
        EightDotThree.make_8dot3_name = staticmethod(_make_gbk_safe_8dot3_name)
        EightDotThree.is_8dot3_conform = staticmethod(_is_gbk_safe_8dot3_conform)
        try:
            yield
        finally:
            EightDotThree.make_8dot3_name = staticmethod(_ORIGINAL_MAKE_8DOT3_NAME)
            EightDotThree.is_8dot3_conform = staticmethod(_ORIGINAL_IS_8DOT3_CONFORM)


def normalize_nand_path(value: object, *, allow_root: bool = True) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if text[:2].upper() == "A:":
        text = text[2:]
    path = PurePosixPath("/" + text.lstrip("/"))
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError(f"invalid NAND path: {value!r}")
    normalized = "/" + "/".join(path.parts[1:])
    if normalized == "/" and not allow_root:
        raise ValueError("the NAND root cannot be modified")
    return normalized


def join_nand_path(parent: object, name: object) -> str:
    base = normalize_nand_path(parent)
    leaf = str(name or "").strip()
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\\" in leaf or "\0" in leaf:
        raise ValueError(f"invalid NAND name: {name!r}")
    return normalize_nand_path(base.rstrip("/") + "/" + leaf, allow_root=False)


def _nand_block_map(path: Path) -> dict[int, tuple[int, int]]:
    result = scan_ftl_image(path)
    mapping = {
        logical: (record.sequence or 0, record.physical)
        for logical, record in result.mapping.items()
    }
    if 0 not in mapping:
        raise ValueError(f"NAND image has no logical FTL block zero: {path}")
    return mapping


def _fat_image_size(header: bytes) -> int:
    offset = DEFAULT_VOLUME_LBA * 512
    boot = header[offset : offset + 512]
    if len(boot) != 512 or boot[510:512] != b"\x55\xaa":
        raise ValueError("missing FAT16 boot sector at logical LBA 0x20")
    bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
    total16 = struct.unpack_from("<H", boot, 19)[0]
    total32 = struct.unpack_from("<I", boot, 32)[0]
    hidden = struct.unpack_from("<I", boot, 28)[0]
    if bytes_per_sector != 512 or not (total16 or total32):
        raise ValueError("unsupported FAT16 geometry")
    return (hidden + (total16 or total32)) * bytes_per_sector


def extract_logical_fat_image(nand_path: Path, output_path: Path) -> int:
    nand = nand_path.resolve()
    mapping = _nand_block_map(nand)
    logical_count = max(mapping) + 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with nand.open("rb") as source, output_path.open("wb") as output:
        for logical in range(logical_count):
            row = mapping.get(logical)
            if row is None:
                raise ValueError(f"missing logical NAND block 0x{logical:x}")
            _sequence, physical = row
            physical_start = physical * RAW_BLOCK_SIZE
            for page in range(PAGES_PER_BLOCK):
                source.seek(physical_start + page * PAGE_STRIDE)
                data = source.read(PAGE_SIZE)
                if len(data) != PAGE_SIZE:
                    raise IOError("short NAND page while extracting FAT image")
                output.write(data)
        output.flush()
        os.fsync(output.fileno())
    with output_path.open("rb") as stream:
        header = stream.read((DEFAULT_VOLUME_LBA + 1) * 512)
    size = _fat_image_size(header)
    if size > output_path.stat().st_size:
        raise ValueError(f"FAT image requires {size} bytes but FTL map exposes {output_path.stat().st_size}")
    with output_path.open("r+b") as output:
        output.truncate(size)
    return size


def inject_logical_fat_image(nand_path: Path, fat_path: Path) -> None:
    inject_logical_fat_image_validated(nand_path, fat_path)


def _ftl_record_signature(result) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            record.physical,
            record.kind,
            record.sequence,
            record.logical,
            record.tail,
            record.last_valid_page,
            record.marker,
        )
        for record in result.records
    )


def _validate_ftl_scan(result, *, label: str) -> None:
    if 0 not in result.mapping:
        raise ValueError(f"{label} NAND has no logical FTL block zero")
    anomalies = [
        record
        for record in result.records
        if record.kind in {"torn", "invalid"}
    ]
    if anomalies:
        first = anomalies[0]
        raise ValueError(
            f"{label} NAND has {first.kind} FTL block 0x{first.physical:x}: "
            f"{first.reason or 'invalid FTL state'}"
        )


def _validate_boot_ecc(
    nand_path: Path,
    result,
    *,
    label: str,
) -> int:
    """Check logical block zero metadata and the FAT boot-sector page."""

    checked_pages = 0
    logical = 0
    record = result.mapping[logical]
    pages = {0, DEFAULT_VOLUME_LBA * 512 // PAGE_SIZE}
    if (
        record.last_valid_page is not None
        and 0 <= record.last_valid_page < PAGES_PER_BLOCK
    ):
        pages.add(record.last_valid_page)
    with nand_path.open("rb") as stream:
        for page_in_block in pages:
            physical_page = record.physical * PAGES_PER_BLOCK + page_in_block
            stream.seek(physical_page * PAGE_STRIDE)
            page_data = stream.read(PAGE_SIZE)
            page_oob = stream.read(SPARE_SIZE)
            if len(page_data) != PAGE_SIZE or len(page_oob) != SPARE_SIZE:
                raise IOError(
                    f"short NAND page while checking ECC at physical page "
                    f"0x{physical_page:x}"
                )
            expected = jz4740_page_oob_ecc(page_data, offset=4)
            if page_oob[4 : len(expected)] != expected[4:]:
                raise ValueError(
                    f"{label} NAND has invalid RS ECC at physical page "
                    f"0x{physical_page:x} (logical block 0x{logical:x}); "
                    "repair the image with tools/stamp_nand_ecc.py"
                )
            checked_pages += 1
    return checked_pages


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(1024 * 1024)
            right_chunk = right_stream.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _validate_nand_candidate(
    reference: Path,
    candidate: Path,
    expected_fat: Path,
    validator: Callable[[PyFatFS], object] | None,
) -> None:
    reference_scan = scan_ftl_image(reference)
    candidate_scan = scan_ftl_image(candidate)
    _validate_ftl_scan(reference_scan, label="source")
    _validate_ftl_scan(candidate_scan, label="candidate")
    _validate_boot_ecc(
        reference,
        reference_scan,
        label="source",
    )
    _validate_boot_ecc(
        candidate,
        candidate_scan,
        label="candidate",
    )
    if reference_scan.block_count != candidate_scan.block_count:
        raise ValueError("candidate NAND geometry changed during FAT injection")
    if _ftl_record_signature(reference_scan) != _ftl_record_signature(candidate_scan):
        raise ValueError("candidate NAND changed FTL mapping metadata")

    verify_fat = expected_fat.with_name(f".{expected_fat.name}.verify")
    try:
        extract_logical_fat_image(candidate, verify_fat)
        if not _files_equal(expected_fat, verify_fat):
            raise ValueError("candidate NAND FAT data does not match the requested mutation")
        fs = _open_fat(verify_fat, read_only=True)
        try:
            if not fs.isdir("/"):
                raise ValueError("candidate NAND FAT root directory is unavailable")
            if validator is not None:
                validator(fs)
        finally:
            fs.close()
    finally:
        verify_fat.unlink(missing_ok=True)


def validate_nand_image(nand_path: Path) -> dict[str, int]:
    """Validate the firmware FTL map and mountable logical FAT volume."""

    nand = nand_path.resolve()
    result = scan_ftl_image(nand)
    _validate_ftl_scan(result, label="selected")
    ecc_pages_checked = _validate_boot_ecc(
        nand,
        result,
        label="selected",
    )
    with tempfile.TemporaryDirectory(prefix="bbk9588-nand-validate-") as tmp:
        fat_path = Path(tmp) / "nand-fat.img"
        fat_size = extract_logical_fat_image(nand, fat_path)
        fs = _open_fat(fat_path, read_only=True)
        try:
            if not fs.isdir("/"):
                raise ValueError("selected NAND FAT root directory is unavailable")
            fs.listdir("/")
        finally:
            fs.close()
    return {
        "block_count": result.block_count,
        "mapped_logical_blocks": len(result.mapping),
        "fat_size": fat_size,
        "ecc_pages_checked": ecc_pages_checked,
    }


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inject_logical_fat_image_validated(
    nand_path: Path,
    fat_path: Path,
    *,
    validator: Callable[[PyFatFS], object] | None = None,
) -> None:
    nand = nand_path.resolve()
    fat = fat_path.resolve()
    mapping = _nand_block_map(nand)
    fat_size = fat.stat().st_size
    logical_count = (fat_size + LOGICAL_BLOCK_SIZE - 1) // LOGICAL_BLOCK_SIZE
    missing = [logical for logical in range(logical_count) if logical not in mapping]
    if missing:
        raise ValueError(f"NAND FTL map is missing logical block 0x{missing[0]:x}")
    temporary = nand.with_name(f".{nand.name}.{os.getpid()}.files.tmp")
    try:
        shutil.copy2(nand, temporary)
        with fat.open("rb") as source, temporary.open("r+b") as output:
            remaining = fat_size
            for logical in range(logical_count):
                _sequence, physical = mapping[logical]
                for page in range(PAGES_PER_BLOCK):
                    if remaining <= 0:
                        break
                    data = source.read(min(PAGE_SIZE, remaining))
                    if not data:
                        raise IOError("short FAT image while injecting NAND")
                    if len(data) < PAGE_SIZE:
                        data += b"\x00" * (PAGE_SIZE - len(data))
                    page_offset = physical * RAW_BLOCK_SIZE + page * PAGE_STRIDE
                    output.seek(page_offset)
                    previous = output.read(PAGE_SIZE)
                    if len(previous) != PAGE_SIZE:
                        raise IOError("short NAND page while injecting FAT image")
                    if previous != data:
                        output.seek(page_offset)
                        output.write(data)
                        parity = jz4740_page_oob_ecc(data, offset=4)
                        output.seek(page_offset + PAGE_SIZE + 4)
                        output.write(parity[4:])
                    remaining -= min(PAGE_SIZE, remaining)
            if remaining:
                raise IOError(f"FAT injection left {remaining} bytes unwritten")
            output.flush()
            os.fsync(output.fileno())
        _validate_nand_candidate(nand, temporary, fat, validator)
        os.replace(temporary, nand)
        _fsync_parent_directory(nand.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _open_fat(path: Path, *, read_only: bool) -> PyFatFS:
    return PyFatFS(
        str(path),
        encoding="gbk",
        offset=DEFAULT_VOLUME_LBA * 512,
        preserve_case=True,
        read_only=read_only,
    )


def list_fat_directory(fat_path: Path, directory: object = "/") -> dict[str, object]:
    normalized = normalize_nand_path(directory)
    fs = _open_fat(fat_path, read_only=True)
    try:
        entries: list[dict[str, Any]] = []
        for info in fs.scandir(normalized, namespaces=["basic", "details"]):
            name = str(info.name)
            if name in {".", ".."}:
                continue
            is_dir = bool(info.is_dir)
            entries.append(
                {
                    "name": name,
                    "path": join_nand_path(normalized, name),
                    "is_dir": is_dir,
                    "size": 0 if is_dir else int(info.size or 0),
                    "modified": (
                        info.modified.isoformat()
                        if getattr(info, "modified", None) is not None
                        else None
                    ),
                }
            )
        entries.sort(key=lambda item: (not bool(item["is_dir"]), str(item["name"]).casefold()))
        return {"path": normalized, "entries": entries}
    finally:
        fs.close()


def list_nand_directory(nand_path: Path, directory: object = "/") -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="bbk9588-fat-list-") as tmp:
        fat_path = Path(tmp) / "nand-fat.img"
        extract_logical_fat_image(nand_path, fat_path)
        return list_fat_directory(fat_path, directory)


def read_fat_file(fat_path: Path, file_path: object) -> tuple[str, bytes]:
    normalized = normalize_nand_path(file_path, allow_root=False)
    fs = _open_fat(fat_path, read_only=True)
    try:
        if not fs.isfile(normalized):
            raise FileNotFoundError(f"NAND file does not exist: {normalized}")
        return PurePosixPath(normalized).name, fs.readbytes(normalized)
    finally:
        fs.close()


def read_nand_file(nand_path: Path, file_path: object) -> tuple[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="bbk9588-fat-read-") as tmp:
        fat_path = Path(tmp) / "nand-fat.img"
        extract_logical_fat_image(nand_path, fat_path)
        return read_fat_file(fat_path, file_path)


def mutate_nand_files(
    nand_path: Path,
    operation: Callable[[PyFatFS], object],
    *,
    validator: Callable[[PyFatFS], object] | None = None,
) -> object:
    with tempfile.TemporaryDirectory(prefix="bbk9588-fat-write-") as tmp:
        fat_path = Path(tmp) / "nand-fat.img"
        extract_logical_fat_image(nand_path, fat_path)
        fs = _open_fat(fat_path, read_only=False)
        try:
            with _gbk_safe_short_names():
                result = operation(fs)
        finally:
            fs.close()
        with fat_path.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        inject_logical_fat_image_validated(
            nand_path,
            fat_path,
            validator=validator,
        )
        return result
