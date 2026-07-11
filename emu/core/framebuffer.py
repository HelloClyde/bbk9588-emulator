"""Framebuffer rendering helpers for BBK 9588 RGB565 dumps."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Protocol

RAM_BASE = 0x80000000


class FramebufferReader(Protocol):
    def _read_block_va_safe(self, va: int, size: int) -> bytes | None: ...


_RGB565_LUT_RGB: tuple[tuple[int, int, int], ...] | None = None
_RGB565_LUT_BGR: tuple[tuple[int, int, int], ...] | None = None


def _rgb565_lut(bgr: bool) -> tuple[tuple[int, int, int], ...]:
    global _RGB565_LUT_RGB, _RGB565_LUT_BGR
    cached = _RGB565_LUT_BGR if bgr else _RGB565_LUT_RGB
    if cached is not None:
        return cached
    table: list[tuple[int, int, int]] = []
    for px in range(0x10000):
        r = ((px >> 11) & 0x1F) * 255 // 31
        g = ((px >> 5) & 0x3F) * 255 // 63
        b = (px & 0x1F) * 255 // 31
        table.append((b, g, r) if bgr else (r, g, b))
    cached = tuple(table)
    if bgr:
        _RGB565_LUT_BGR = cached
    else:
        _RGB565_LUT_RGB = cached
    return cached


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def png_bytes_from_rgb(width: int, height: int, rgb: bytes) -> bytes:
    if len(rgb) != width * height * 3:
        raise ValueError("RGB buffer size does not match output dimensions")
    rows = bytearray()
    row_size = width * 3
    for y in range(height):
        rows.append(0)
        start = y * row_size
        rows.extend(rgb[start : start + row_size])
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )
    return png


def write_rgb_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    png = png_bytes_from_rgb(width, height, rgb)
    path.write_bytes(png)


def write_rgb_ppm(path: Path, width: int, height: int, rgb: bytes) -> None:
    if len(rgb) != width * height * 3:
        raise ValueError("RGB buffer size does not match output dimensions")
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + rgb)


def orient_rgb(
    rgb: bytes,
    width: int,
    height: int,
    orientation: str,
) -> tuple[int, int, bytes]:
    if orientation == "raw":
        return width, height, rgb
    if orientation not in {"rot180", "cw90", "ccw90", "hflip", "vflip"}:
        raise ValueError(f"unsupported framebuffer orientation: {orientation}")

    src = memoryview(rgb)
    if orientation in {"rot180", "hflip", "vflip"}:
        out = bytearray(width * height * 3)
        for y in range(height):
            for x in range(width):
                if orientation == "rot180":
                    src_x = width - 1 - x
                    src_y = height - 1 - y
                elif orientation == "hflip":
                    src_x = width - 1 - x
                    src_y = y
                else:
                    src_x = x
                    src_y = height - 1 - y
                src_i = (src_y * width + src_x) * 3
                dst_i = (y * width + x) * 3
                out[dst_i : dst_i + 3] = src[src_i : src_i + 3]
        return width, height, bytes(out)

    out_w = height
    out_h = width
    out = bytearray(out_w * out_h * 3)
    for y in range(out_h):
        for x in range(out_w):
            if orientation == "cw90":
                src_x = y
                src_y = height - 1 - x
            else:
                src_x = width - 1 - y
                src_y = x
            src_i = (src_y * width + src_x) * 3
            dst_i = (y * out_w + x) * 3
            out[dst_i : dst_i + 3] = src[src_i : src_i + 3]
    return out_w, out_h, bytes(out)


def render_rgb565_framebuffer(
    emu: object,
    addr: int,
    offset_bytes: int,
    width: int,
    height: int,
    stride_pixels: int,
    pixel_format: str,
    orientation: str,
) -> tuple[dict[str, object], bytes]:
    if width <= 0 or height <= 0 or stride_pixels < width or offset_bytes < 0:
        raise ValueError("invalid framebuffer dimensions")

    phys = (addr & 0x1FFFFFFF if addr >= RAM_BASE else addr) + offset_bytes
    raw = bytes(emu.uc.mem_read(phys, stride_pixels * height * 2))
    return rgb565_raw_to_info_rgb(
        raw,
        addr,
        offset_bytes,
        width,
        height,
        stride_pixels,
        pixel_format,
        orientation,
    )


def rgb565_raw_to_info_rgb(
    raw: bytes,
    addr: int,
    offset_bytes: int,
    width: int,
    height: int,
    stride_pixels: int,
    pixel_format: str,
    orientation: str,
) -> tuple[dict[str, object], bytes]:
    if width <= 0 or height <= 0 or stride_pixels < width or offset_bytes < 0:
        raise ValueError("invalid framebuffer dimensions")
    if pixel_format not in {"rgb565", "bgr565", "rgb565-be", "bgr565-be"}:
        raise ValueError(f"unsupported framebuffer format: {pixel_format}")
    required = stride_pixels * height * 2
    if len(raw) < required:
        raise ValueError("RGB565 buffer size does not match framebuffer dimensions")

    big_endian = pixel_format.endswith("-be")
    bgr = pixel_format.startswith("bgr")
    rgb_lut = _rgb565_lut(bgr)
    rgb = bytearray(width * height * 3)
    nonzero = 0
    unique: set[int] = set()
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1

    out_i = 0
    for y in range(height):
        row = y * stride_pixels * 2
        for x in range(width):
            i = row + x * 2
            if big_endian:
                px = (raw[i] << 8) | raw[i + 1]
            else:
                px = raw[i] | (raw[i + 1] << 8)
            unique.add(px)
            if px:
                nonzero += 1
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
            r, g, b = rgb_lut[px]
            rgb[out_i] = r
            rgb[out_i + 1] = g
            rgb[out_i + 2] = b
            out_i += 3

    out_width, out_height, out_rgb = orient_rgb(bytes(rgb), width, height, orientation)

    bbox = None if max_x < 0 else [min_x, min_y, max_x, max_y]
    return {
        "addr": f"0x{addr:08x}",
        "offset_bytes": offset_bytes,
        "format": pixel_format,
        "width": width,
        "height": height,
        "stride_pixels": stride_pixels,
        "orientation": orientation,
        "output_width": out_width,
        "output_height": out_height,
        "nonzero_pixels": nonzero,
        "unique_pixel_values": len(unique),
        "nonzero_bbox": bbox,
    }, out_rgb


def dump_rgb565_framebuffer(
    emu: object,
    path: Path,
    addr: int,
    offset_bytes: int,
    width: int,
    height: int,
    stride_pixels: int,
    pixel_format: str,
    orientation: str,
) -> dict[str, object]:
    info, out_rgb = render_rgb565_framebuffer(
        emu,
        addr,
        offset_bytes,
        width,
        height,
        stride_pixels,
        pixel_format,
        orientation,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    out_width = int(info["output_width"])
    out_height = int(info["output_height"])
    if path.suffix.lower() == ".png":
        write_rgb_png(path, out_width, out_height, out_rgb)
    else:
        write_rgb_ppm(path, out_width, out_height, out_rgb)

    info["path"] = str(path)
    return info


def scan_rgb565_framebuffers(
    emu: object,
    width: int,
    height: int,
    stride_pixels: int,
    topn: int = 16,
) -> list[dict[str, object]]:
    row_bytes = stride_pixels * 2
    window_bytes = row_bytes * height
    ranges = [
        (0x00400000, 0x00A00000),
        (0x01F00000, 0x02080000),
    ]
    candidates: list[dict[str, object]] = []
    for start, end in ranges:
        last = min(end, emu.ram_size) - window_bytes
        if last <= start:
            continue
        for phys in range(start, last + 1, 0x1000):
            try:
                data = bytes(emu.uc.mem_read(phys, window_bytes))
            except Exception:
                continue
            nonzero = 0
            unique: set[int] = set()
            for off in range(0, len(data), 2):
                value = data[off] | (data[off + 1] << 8)
                if value:
                    nonzero += 1
                    if len(unique) < 256:
                        unique.add(value)
            if nonzero == 0:
                continue
            candidates.append(
                {
                    "phys": f"0x{phys:08x}",
                    "kseg0": f"0x{phys | 0x80000000:08x}",
                    "kseg1": f"0x{phys | 0xA0000000:08x}",
                    "nonzero_pixels": nonzero,
                    "unique_sample": len(unique),
                }
            )
    candidates.sort(key=lambda row: (int(row["nonzero_pixels"]), int(row["unique_sample"])), reverse=True)
    return candidates[:topn]
