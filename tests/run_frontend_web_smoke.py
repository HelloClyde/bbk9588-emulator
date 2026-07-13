#!/usr/bin/env python3
"""Exercise the BBK 9588 frontend through HTTP and WebSocket like a user."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typing import Callable

from emu.core.framebuffer import png_bytes_from_rgb, rgb565_raw_to_info_rgb
from emu.web.frontend_ws import WebSocketFrameReader, encode_ws_frame

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DEFAULT_NAND = ROOT / "runtime" / "bbk9588_nand.bin"
WS_RAW_FRAME_MAGIC = b"BBKRAW1\0"
WS_RAW_FRAME_HEADER_SIZE = 20
WS_RAW_FRAME_FORMAT_RGB565 = 1
MENU_MIN_NONZERO_PIXELS = 25000
MENU_MIN_UNIQUE_PIXEL_VALUES = 2000
RENDERED_MIN_NONZERO_PIXELS = 15000
RENDERED_MIN_UNIQUE_PIXEL_VALUES = 500


def ws_raw_frame_seq(payload: bytes) -> int | None:
    if payload.startswith(WS_RAW_FRAME_MAGIC) and len(payload) >= WS_RAW_FRAME_HEADER_SIZE:
        return int.from_bytes(payload[8:12], "little")
    return None


def ws_raw_frame_pixels(payload: bytes) -> tuple[int, int, int, int, bytes]:
    if not payload.startswith(WS_RAW_FRAME_MAGIC) or len(payload) < WS_RAW_FRAME_HEADER_SIZE:
        raise ValueError("WebSocket frame is not raw RGB565")
    seq = int.from_bytes(payload[8:12], "little")
    width = int.from_bytes(payload[12:14], "little")
    height = int.from_bytes(payload[14:16], "little")
    stride = int.from_bytes(payload[16:18], "little")
    pixel_format = int.from_bytes(payload[18:20], "little")
    if pixel_format != WS_RAW_FRAME_FORMAT_RGB565:
        raise ValueError(f"unsupported raw WS frame format {pixel_format}")
    if width <= 0 or height <= 0 or stride < width:
        raise ValueError(f"invalid raw WS frame geometry {width}x{height} stride={stride}")
    pixels = payload[WS_RAW_FRAME_HEADER_SIZE:]
    expected = stride * height * 2
    if len(pixels) != expected:
        raise ValueError(f"raw WS frame has {len(pixels)} bytes, expected {expected}")
    return seq, width, height, stride, pixels


def rgb565_changed_pixels(before: bytes, after: bytes) -> int:
    if len(before) != len(after) or len(before) % 2:
        raise ValueError("RGB565 frames must have matching even byte lengths")
    return sum(
        before[offset : offset + 2] != after[offset : offset + 2]
        for offset in range(0, len(before), 2)
    )


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def http_json(host: str, port: int, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    conn = http.client.HTTPConnection(host, port, timeout=30)
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=raw, headers=headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    if res.status >= 400:
        raise RuntimeError(f"{method} {path} returned HTTP {res.status}: {data[:200]!r}")
    return json.loads(data.decode("utf-8") or "{}")


def http_bytes(host: str, port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.request("GET", path)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


def qemu_display_queue_drained(status: dict[str, object]) -> bool:
    queue = status.get("display_event_queue")
    if not isinstance(queue, dict):
        return False
    if not queue.get("mapped"):
        return False
    try:
        return int(str(queue.get("read_index", -1)), 0) == int(str(queue.get("write_index", -2)), 0)
    except (TypeError, ValueError):
        return False


def wait_qemu_display_queue_drained(host: str, port: int, timeout: float) -> dict[str, object]:
    deadline = time.time() + max(0.0, timeout)
    last: dict[str, object] = {}
    while time.time() < deadline:
        last = http_json(host, port, "GET", "/api/status?detail=full")
        if qemu_display_queue_drained(last):
            return last
        time.sleep(0.1)
    return last


def ws_frame_payload_to_png(payload: bytes, orientation: str) -> tuple[bytes, str]:
    if payload.startswith(WS_RAW_FRAME_MAGIC) and len(payload) >= WS_RAW_FRAME_HEADER_SIZE:
        seq = int.from_bytes(payload[8:12], "little")
        width = int.from_bytes(payload[12:14], "little")
        height = int.from_bytes(payload[14:16], "little")
        stride = int.from_bytes(payload[16:18], "little")
        pixel_format = int.from_bytes(payload[18:20], "little")
        raw = payload[WS_RAW_FRAME_HEADER_SIZE:]
        if pixel_format != WS_RAW_FRAME_FORMAT_RGB565:
            raise ValueError(f"unsupported raw WS frame format {pixel_format}")
        info, rgb = rgb565_raw_to_info_rgb(
            raw,
            0xA1F82000,
            0,
            width,
            height,
            stride,
            "rgb565",
            orientation,
        )
        info["dirty_seq"] = seq
        return png_bytes_from_rgb(int(info["output_width"]), int(info["output_height"]), rgb), "raw-rgb565"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return payload, "png"
    raise ValueError(f"unknown WS frame payload signature {payload[:12].hex()}")


class WebSocketClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket handshake failed: {response[:200]!r}")
        self.sock.settimeout(0.25)
        self.reader = WebSocketFrameReader(self.sock)
        self.last_status: dict[str, object] = {}
        self.last_frame: bytes | None = None
        self.last_frame_payload: bytes | None = None
        self.last_frame_orientation = "rot180"
        self.last_frame_wire_kind = ""
        self.last_frame_seq: int | None = None
        self.frames = 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send_json(self, msg: dict[str, object]) -> None:
        payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        self.sock.sendall(encode_ws_frame(0x1, payload, mask=os.urandom(4)))

    def send_command_async(self, msg: dict[str, object]) -> int:
        command = dict(msg)
        command_seq = int(time.time() * 1_000_000)
        command["command_seq"] = command_seq
        self.send_json(command)
        return command_seq

    def send_command(self, msg: dict[str, object], timeout: float = 3.0) -> dict[str, object] | None:
        command_seq = self.send_command_async(msg)
        return self.wait_for_command_seq(command_seq, timeout)

    def recv_one(self) -> tuple[str, object] | None:
        try:
            frame = self.reader.read_frame()
        except TimeoutError:
            return None
        except socket.timeout:
            return None
        if frame is None:
            return None
        opcode, payload = frame
        if opcode == 0x1:
            status = json.loads(payload.decode("utf-8"))
            if isinstance(status, dict):
                self.last_status = status
            return "json", status
        if opcode == 0x2:
            orientation = str(self.last_status.get("orientation") or "rot180")
            if payload.startswith(WS_RAW_FRAME_MAGIC) and len(payload) >= WS_RAW_FRAME_HEADER_SIZE:
                self.last_frame_wire_kind = "raw-rgb565"
                self.last_frame_seq = ws_raw_frame_seq(payload)
            elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
                self.last_frame_wire_kind = "png"
                self.last_frame_seq = None
            else:
                self.last_frame_wire_kind = "unknown"
                self.last_frame_seq = None
            self.last_frame_payload = payload
            self.last_frame_orientation = orientation
            self.last_frame = None
            self.frames += 1
            return "frame", payload
        if opcode == 0x8:
            return "close", payload
        return "other", payload

    def current_frame_png(self) -> bytes | None:
        if self.last_frame is not None:
            return self.last_frame
        if self.last_frame_payload is None:
            return None
        self.last_frame, self.last_frame_wire_kind = ws_frame_payload_to_png(
            self.last_frame_payload,
            self.last_frame_orientation,
        )
        return self.last_frame

    def pump(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.recv_one()

    def wait_for(
        self,
        predicate: Callable[[dict[str, object]], bool],
        timeout: float,
        *,
        poll_status: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        deadline = time.time() + timeout
        last_poll = 0.0
        while time.time() < deadline:
            self.recv_one()
            if predicate(self.last_status):
                return self.last_status
            now = time.time()
            if poll_status is not None and now - last_poll >= 2.0:
                self.last_status = poll_status()
                last_poll = now
                if predicate(self.last_status):
                    return self.last_status
        return self.last_status

    def wait_for_new_status(
        self,
        predicate: Callable[[dict[str, object]], bool],
        timeout: float,
        *,
        poll_status: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        deadline = time.time() + timeout
        last_poll = 0.0
        while time.time() < deadline:
            item = self.recv_one()
            if item is not None and item[0] == "json" and predicate(self.last_status):
                return self.last_status
            now = time.time()
            if poll_status is not None and now - last_poll >= 2.0:
                polled = poll_status()
                last_poll = now
                if predicate(polled):
                    self.last_status = polled
                    return polled
        return self.last_status

    def wait_for_frame_after(self, previous_seq: int | None, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.recv_one()
            if item is None or item[0] != "frame":
                continue
            if previous_seq is None:
                return True
            if self.last_frame_seq is None:
                return True
            if self.last_frame_seq != previous_seq:
                return True
        return False

    def wait_for_command_seq(self, command_seq: int, timeout: float) -> dict[str, object] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.recv_one()
            if item is None or item[0] != "json":
                continue
            if self.last_status.get("_ws_command_seq") == command_seq:
                return self.last_status
        return None

    def wait_for_command_seen(
        self,
        command_seq: int,
        timeout: float,
        *,
        poll_status: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        def seen(status: dict[str, object]) -> bool:
            ws = status.get("ws") if isinstance(status, dict) else None
            return isinstance(ws, dict) and ws.get("last_seq") == command_seq

        deadline = time.time() + timeout
        last_poll = 0.0
        while time.time() < deadline:
            item = self.recv_one()
            if item is not None and item[0] == "json" and seen(self.last_status):
                return self.last_status
            now = time.time()
            if poll_status is not None and now - last_poll >= 0.15:
                self.last_status = poll_status()
                last_poll = now
                if seen(self.last_status):
                    return self.last_status
        return self.last_status if seen(self.last_status) else None

    def wait_for_queue_drained(
        self,
        queue_name: str,
        timeout: float,
        *,
        poll_status: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        def drained(status: dict[str, object]) -> bool:
            return int(status.get(queue_name) or 0) == 0

        deadline = time.time() + timeout
        last_poll = 0.0
        while time.time() < deadline:
            item = self.recv_one()
            if item is not None and item[0] == "json" and drained(self.last_status):
                return self.last_status
            now = time.time()
            if poll_status is not None and now - last_poll >= 0.15:
                self.last_status = poll_status()
                last_poll = now
                if drained(self.last_status):
                    return self.last_status
        return self.last_status


def start_frontend(args: argparse.Namespace, port: int) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable,
        str(ROOT / "emu" / "app.py"),
        "--host",
        args.host,
        "--port",
        str(port),
        "--boot-mode",
        str(getattr(args, "boot_mode", "nand")),
        "--frame-push-min-interval",
        str(args.frame_push_min_interval),
        "--quiet",
    ]
    nand_image = getattr(args, "nand_image", None)
    if nand_image is not None:
        cmd += ["--nand-image", str(nand_image)]
    profile_out = getattr(args, "frontend_profile_out", None)
    if profile_out is not None:
        cmd += ["--profile-out", str(profile_out)]
    cmd += ["--backend", "qemu"]
    for option_name, cli_name in (
        ("qemu", "--qemu"),
        ("qemu_machine", "--qemu-machine"),
        ("qemu_cpu", "--qemu-cpu"),
        ("qemu_accel", "--qemu-accel"),
        ("qemu_gdb", "--qemu-gdb"),
        ("qemu_timeout", "--qemu-timeout"),
    ):
        value = getattr(args, option_name, None)
        if value is not None:
            cmd += [cli_name, str(value)]
    firmware_patches = getattr(args, "qemu_firmware_patch", None)
    if firmware_patches is not None:
        for value in firmware_patches:
            cmd += ["--qemu-firmware-patch", str(value)]
    for value in getattr(args, "qemu_machine_option", []) or []:
        cmd += ["--qemu-machine-option", str(value)]
    for value in getattr(args, "qemu_extra_arg", []) or []:
        cmd += ["--qemu-extra-arg", str(value)]
    return subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_http(host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            http_json(host, port, "GET", "/api/status")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"frontend did not become ready: {last_error}")


def summarize_status(status: dict[str, object]) -> dict[str, object]:
    fb = status.get("framebuffer") if isinstance(status.get("framebuffer"), dict) else {}
    job = status.get("job") if isinstance(status.get("job"), dict) else {}
    scheduler = status.get("scheduler") if isinstance(status.get("scheduler"), dict) else {}
    return {
        "running": status.get("running"),
        "pc": status.get("pc"),
        "stop_reason": status.get("stop_reason"),
        "frontend_input_calibration_stage": status.get("frontend_input_calibration_stage"),
        "frontend_input_calibration_stage_label": status.get("frontend_input_calibration_stage_label"),
        "pending_touches": status.get("pending_touches"),
        "pending_keys": status.get("pending_keys"),
        "input_wake_count": status.get("input_wake_count"),
        "nonzero_pixels": fb.get("nonzero_pixels"),
        "unique_pixel_values": fb.get("unique_pixel_values"),
        "job": {
            "name": job.get("name"),
            "status": job.get("status"),
            "done_steps": job.get("done_steps"),
            "observed_insn_delta": job.get("observed_insn_delta"),
            "steps_per_second": job.get("steps_per_second"),
            "requested_steps_per_second": job.get("requested_steps_per_second"),
        },
        "perf": status.get("perf"),
        "memcpy_bulk_callers": status.get("memcpy_bulk_callers"),
        "store_delay_branch_counts": status.get("store_delay_branch_counts"),
        "on_code_dispatch_counts": status.get("on_code_dispatch_counts"),
        "block_dispatch_counts": status.get("block_dispatch_counts"),
        "recoveries": status.get("recoveries"),
        "trace_pc": status.get("trace_pc"),
        "scheduler": scheduler,
        "tasks": status.get("tasks"),
        "event_queue": status.get("event_queue"),
        "display_event_queue": status.get("display_event_queue"),
        "recent_event_queue_snapshots": status.get("recent_event_queue_snapshots"),
        "recent_gui_ring_pump_events": status.get("recent_gui_ring_pump_events"),
        "frame_push": status.get("frame_push"),
        "ws": status.get("ws"),
    }


def looks_like_menu(status: dict[str, object]) -> bool:
    fb = status.get("framebuffer") if isinstance(status.get("framebuffer"), dict) else {}
    return (
        int(status.get("frontend_input_calibration_stage") or 0) >= 12
        and int(fb.get("nonzero_pixels") or 0) >= MENU_MIN_NONZERO_PIXELS
        and int(fb.get("unique_pixel_values") or 0)
        >= MENU_MIN_UNIQUE_PIXEL_VALUES
    )


def looks_like_menu_family(
    status: dict[str, object], *, require_calibration: bool = True
) -> bool:
    fb = status.get("framebuffer") if isinstance(status.get("framebuffer"), dict) else {}
    nonzero = int(fb.get("nonzero_pixels") or 0)
    unique = int(fb.get("unique_pixel_values") or 0)
    return (
        (
            not require_calibration
            or int(status.get("frontend_input_calibration_stage") or 0) >= 12
        )
        and nonzero >= MENU_MIN_NONZERO_PIXELS
        and unique >= MENU_MIN_UNIQUE_PIXEL_VALUES
    )


def looks_like_rendered_screen(status: dict[str, object]) -> bool:
    fb = status.get("framebuffer") if isinstance(status.get("framebuffer"), dict) else {}
    return (
        int(fb.get("nonzero_pixels") or 0) >= RENDERED_MIN_NONZERO_PIXELS
        and int(fb.get("unique_pixel_values") or 0)
        >= RENDERED_MIN_UNIQUE_PIXEL_VALUES
    )


def status_pc_value(status: dict[str, object]) -> int | None:
    qemu = status.get("qemu") if isinstance(status.get("qemu"), dict) else {}
    sample = qemu.get("register_sample") if isinstance(qemu.get("register_sample"), dict) else {}
    for value in (status.get("pc"), qemu.get("pc"), sample.get("pc")):
        if value is None:
            continue
        try:
            return int(str(value), 0) & 0xFFFFFFFF
        except (TypeError, ValueError):
            continue
    return None


def status_pc_in_uboot(status: dict[str, object]) -> bool:
    pc = status_pc_value(status)
    return pc is not None and 0x80900000 <= pc < 0x80A00000


def status_pc_in_c200(status: dict[str, object]) -> bool:
    pc = status_pc_value(status)
    return pc is not None and 0x80000000 <= pc < 0x80900000


def write_png(path: Path, data: bytes | None) -> str | None:
    if not data:
        return None
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def sample_raw_frame_stability(
    ws: WebSocketClient,
    *,
    sample_count: int,
    frame_timeout: float,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    pixel_frames: list[bytes] = []
    ws.pump(max(0.25, min(frame_timeout, 1.0)))
    count = max(2, sample_count)
    for index in range(count):
        advanced = False
        if index == 0 and ws.last_frame_payload is None:
            advanced = ws.wait_for_frame_after(None, frame_timeout)
        elif index > 0:
            advanced = ws.wait_for_frame_after(ws.last_frame_seq, frame_timeout)
        payload = ws.last_frame_payload
        if payload is None:
            raise RuntimeError("WebSocket did not provide a framebuffer frame")
        seq, width, height, stride, pixels = ws_raw_frame_pixels(payload)
        pixel_frames.append(pixels)
        records.append(
            {
                "seq": seq,
                "sha256": hashlib.sha256(pixels).hexdigest(),
                "frame_advanced": advanced,
                "width": width,
                "height": height,
                "stride": stride,
            }
        )

    total_pixels = len(pixel_frames[0]) // 2
    changed_pixels = [
        rgb565_changed_pixels(before, after)
        for before, after in zip(pixel_frames, pixel_frames[1:])
    ]
    changed_ratios = [changed / total_pixels for changed in changed_pixels]
    return {
        "samples": records,
        "changed_pixels": changed_pixels,
        "changed_ratios": [round(ratio, 6) for ratio in changed_ratios],
        "max_changed_ratio": round(max(changed_ratios, default=0.0), 6),
        "total_pixels": total_pixels,
    }
def fetch_screen_digest(host: str, port: int) -> tuple[int, bytes, str]:
    status_code, png = http_bytes(host, port, "/screen.png")
    digest = hashlib.sha256(png).hexdigest() if status_code == 200 else ""
    return status_code, png, digest


def wait_screen_digest(
    host: str,
    port: int,
    predicate: Callable[[str], bool],
    timeout: float,
) -> tuple[int, bytes, str]:
    deadline = time.time() + timeout
    last: tuple[int, bytes, str] = (0, b"", "")
    while time.time() < deadline:
        last = fetch_screen_digest(host, port)
        if predicate(last[2]):
            return last
        time.sleep(0.4)
    return last


def current_ws_screen_digest(ws: WebSocketClient) -> tuple[int, bytes, str]:
    png = ws.current_frame_png() or b""
    digest = hashlib.sha256(png).hexdigest() if png else ""
    return (200 if png else 0), png, digest


def wait_ws_screen_digest(
    ws: WebSocketClient,
    predicate: Callable[[str], bool],
    timeout: float,
) -> tuple[int, bytes, str]:
    last = current_ws_screen_digest(ws)
    if predicate(last[2]):
        return last
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = ws.recv_one()
        if item is None:
            continue
        kind, payload = item
        if kind != "frame":
            continue
        png = ws.current_frame_png() or b""
        digest = hashlib.sha256(png).hexdigest() if png else ""
        last = (200 if png else 0), png, digest
        if predicate(digest):
            return last
    return last


def tap(
    ws: WebSocketClient,
    x: int,
    y: int,
    *,
    poll_status: Callable[[], dict[str, object]] | None = None,
) -> None:
    base = {
        "op": "touch",
        "display_x": x,
        "display_y": y,
        "display_width": 240,
        "display_height": 320,
        "advance": False,
        "run": True,
    }
    down = dict(base)
    down["down"] = True
    down["phase"] = "down"
    down_seq = ws.send_command_async(down)
    ws.wait_for_command_seen(down_seq, 3, poll_status=poll_status)
    ws.wait_for_queue_drained("pending_touches", 5, poll_status=poll_status)
    up = dict(base)
    up["down"] = False
    up["phase"] = "up"
    up_seq = ws.send_command_async(up)
    ws.wait_for_command_seen(up_seq, 3, poll_status=poll_status)
    ws.wait_for_queue_drained("pending_touches", 5, poll_status=poll_status)


def key_press(
    ws: WebSocketClient,
    code: int,
    *,
    poll_status: Callable[[], dict[str, object]] | None = None,
) -> None:
    down_seq = ws.send_command_async({"op": "key", "code": code, "down": True, "advance": False, "run": True})
    ws.wait_for_command_seen(down_seq, 3, poll_status=poll_status)
    ws.wait_for_queue_drained("pending_keys", 5, poll_status=poll_status)
    up_seq = ws.send_command_async({"op": "key", "code": code, "down": False, "advance": False, "run": True})
    ws.wait_for_command_seen(up_seq, 3, poll_status=poll_status)
    ws.wait_for_queue_drained("pending_keys", 5, poll_status=poll_status)


def _write_summary(
    ns: argparse.Namespace,
    port: int,
    start: float,
    menu_elapsed_seconds: float | None,
    failures: list[str],
    screenshots: dict[str, dict[str, object]],
    interactions: list[dict[str, object]],
    logs: dict[str, object],
    ws: WebSocketClient | None,
) -> int:
    elapsed = time.time() - start
    summary = {
        "ok": not failures,
        "host": ns.host,
        "port": port,
        "used_existing": ns.use_existing,
        "elapsed_seconds": round(elapsed, 3),
        "menu_elapsed_seconds": None if menu_elapsed_seconds is None else round(menu_elapsed_seconds, 3),
        "failures": failures,
        "screenshots": screenshots,
        "last_frame_wire_kind": None if ws is None else ws.last_frame_wire_kind,
        "interactions": interactions,
        "log_count": logs.get("count"),
        "recent_logs": logs.get("events", []),
    }
    json_path = ns.out_dir / f"{ns.prefix}_summary.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report_path = ns.out_dir / f"{ns.prefix}_report.md"
    lines = [
        "# Frontend Web Smoke Report",
        "",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'}",
        f"- URL: http://{ns.host}:{port}/",
        f"- Elapsed seconds: {summary['elapsed_seconds']}",
        f"- Menu elapsed seconds: {summary['menu_elapsed_seconds']}",
        f"- Frames received over WS: {ws.frames if ws is not None else 0}",
        f"- Last WS frame wire kind: {None if ws is None else ws.last_frame_wire_kind}",
        f"- Failures: {len(failures)}",
        "",
        "## Interactions",
    ]
    for item in interactions:
        extras: list[str] = []
        if "elapsed_seconds" in item:
            extras.append(f"elapsed={item['elapsed_seconds']}s")
        if "frame_advanced" in item:
            extras.append(f"frame_advanced={item['frame_advanced']}")
        if "frame_seq" in item:
            extras.append(f"frame_seq={item['frame_seq']}")
        prefix = "" if not extras else f" ({', '.join(extras)})"
        lines.append(f"- {item['step']}{prefix}: `{json.dumps(item.get('status', {}), ensure_ascii=False)}`")
    if failures:
        lines += ["", "## Failures"]
        lines += [f"- {failure}" for failure in failures]
    lines += [
        "",
        "## Notes",
        "- This smoke drives the frontend through HTTP and WebSocket, not direct Python state calls.",
        "- Touches use rendered display coordinates and rely on the frontend orientation mapping.",
        "- Category coverage is raw-frame-sequence based; screenshots are converted only when recorded.",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"ok": summary["ok"], "summary": str(json_path), "report": str(report_path), "failures": failures}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a user-like web frontend smoke test over HTTP and WebSocket.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="Use 0 to start a private frontend on a free port.")
    ap.add_argument("--use-existing", action="store_true", help="Connect to an already running frontend instead of starting one.")
    ap.add_argument("--nand-image", type=Path, default=None, help="Override app.py's default NAND image.")
    ap.add_argument("--out-dir", type=Path, default=BUILD)
    ap.add_argument("--prefix", default="hwemu_frontend_web_smoke")
    ap.add_argument("--boot-mode", choices=["nand", "c200", "uboot"], default="nand")
    ap.add_argument("--boot-timeout", type=int, default=480)
    ap.add_argument("--chunk-steps", type=int, default=250000)
    ap.add_argument("--frame-push-min-interval", type=float, default=0.04)
    ap.add_argument("--backend", choices=["qemu"], default="qemu")
    ap.add_argument("--qemu", default=None)
    ap.add_argument("--qemu-machine", dest="qemu_machine", default=None)
    ap.add_argument("--qemu-cpu", dest="qemu_cpu", default=None)
    ap.add_argument("--qemu-accel", dest="qemu_accel", default=None)
    ap.add_argument("--qemu-gdb", dest="qemu_gdb", default=None)
    ap.add_argument("--qemu-timeout", dest="qemu_timeout", type=float, default=None)
    ap.add_argument("--qemu-firmware-patch", action="append", default=None)
    ap.add_argument("--qemu-machine-option", action="append", default=[])
    ap.add_argument("--qemu-extra-arg", action="append", default=[])
    ap.add_argument(
        "--qemu-frontend-input-calibration",
        dest="qemu_frontend_input_calibration",
        action="store_true",
        default=True,
        help="For QEMU backend diagnostics, use the frontend input calibration helper through the QEMU input chardev. Enabled by default.",
    )
    ap.add_argument(
        "--qemu-no-frontend-input-calibration",
        dest="qemu_frontend_input_calibration",
        action="store_false",
        help="Disable the frontend input calibration helper; manual or external touch input must complete calibration.",
    )
    ap.add_argument(
        "--qemu-run-storage-service",
        action="store_true",
        default=False,
        help="For QEMU backend diagnostics, explicitly run the legacy Python/GDB storage service during the smoke.",
    )
    ap.add_argument(
        "--interaction-frame-timeout",
        type=float,
        default=1.0,
        help="Maximum seconds to wait for a new WS frame after a tap when status alone is not enough.",
    )
    ap.add_argument(
        "--frame-stability-samples",
        type=int,
        default=3,
        help="Number of consecutive main-menu raw RGB565 frames used for stability checks.",
    )
    ap.add_argument(
        "--frame-stability-timeout",
        type=float,
        default=2.0,
        help="Maximum seconds to wait for each new frame stability sample.",
    )
    ap.add_argument(
        "--frame-stability-max-change-ratio",
        type=float,
        default=0.20,
        help="Maximum changed RGB565 pixel ratio between consecutive stable-page samples.",
    )
    ns = ap.parse_args(argv)

    ns.out_dir.mkdir(parents=True, exist_ok=True)
    port = ns.port or find_free_port(ns.host)
    proc: subprocess.Popen[bytes] | None = None
    ws: WebSocketClient | None = None
    failures: list[str] = []
    interactions: list[dict[str, object]] = []
    screenshots: dict[str, dict[str, object]] = {}
    logs: dict[str, object] = {}
    menu_elapsed_seconds: float | None = None
    start = time.time()
    try:
        if not ns.use_existing:
            proc = start_frontend(ns, port)
        wait_http(ns.host, port, 30)
        html_status, html = http_bytes(ns.host, port, "/")
        if html_status != 200 or b"<canvas" not in html:
            failures.append("frontend HTML did not load or canvas was missing")

        ws = WebSocketClient(ns.host, port)
        ws.pump(0.2)
        interactions.append({"step": "connect", "status": summarize_status(ws.last_status), "frames": ws.frames})

        reset_started = time.time()
        reset_reply = ws.send_command({"op": "reset"}, timeout=5)
        interactions.append(
            {
                "step": "reset",
                "elapsed_seconds": round(time.time() - reset_started, 3),
                "status": summarize_status(reset_reply or ws.last_status),
            }
        )

        if ns.backend == "qemu":
            if ns.qemu_frontend_input_calibration:
                calibration_reply = http_json(ns.host, port, "POST", "/api/command", {"op": "frontend-input-calibration", "enabled": True})
                interactions.append(
                    {
                        "step": "qemu-enable-frontend-input-calibration",
                        "status": summarize_status(calibration_reply or ws.last_status),
                    }
                )
            else:
                interactions.append(
                    {
                        "step": "qemu-frontend-input-calibration-disabled",
                        "status": summarize_status(ws.last_status),
                    }
                )
            active_status = None
            deadline = time.time() + ns.boot_timeout
            candidate: dict[str, object] = {}
            last_full_probe = 0.0
            while time.time() < deadline:
                compact = http_json(ns.host, port, "GET", "/api/status")
                candidate = compact
                now = time.time()
                stage = int(compact.get("frontend_input_calibration_stage") or 0)
                probe_full = (
                    status_pc_in_c200(compact)
                    or stage > 0
                    or now - last_full_probe >= 15.0
                )
                if not probe_full:
                    time.sleep(0.5)
                    continue
                candidate = http_json(ns.host, port, "GET", "/api/status?detail=full")
                last_full_probe = now
                gui = candidate.get("guest_gui_state") if isinstance(candidate.get("guest_gui_state"), dict) else {}
                stage_ready = (
                    not ns.qemu_frontend_input_calibration
                    or candidate.get("frontend_input_calibration_stage") == 12
                )
                if stage_ready and gui.get("active_object_ready"):
                    active_status = candidate
                    break
                time.sleep(0.5 if status_pc_in_uboot(candidate) else 0.25)
            interactions.append(
                {
                    "step": "qemu-wait-active-object",
                    "status": summarize_status(active_status or candidate if "candidate" in locals() else {}),
                }
            )
            if active_status is None:
                failures.append("qemu frontend input calibration did not reach an active GUI object")
            qemu_status = http_json(ns.host, port, "GET", "/api/status?detail=full")
            qemu_info = qemu_status.get("qemu") if isinstance(qemu_status.get("qemu"), dict) else {}
            register_sample = qemu_info.get("register_sample") if isinstance(qemu_info, dict) else None
            interactions.append(
                {
                    "step": "qemu-status",
                    "status": summarize_status(qemu_status),
                    "qemu": qemu_info,
                }
            )
            if qemu_status.get("backend") != "qemu":
                failures.append("frontend did not report qemu backend")
            if not qemu_status.get("running"):
                failures.append("qemu backend was not running after reset")
            if not isinstance(register_sample, dict) or not register_sample.get("pc"):
                failures.append("qemu backend did not provide a register sample")
            status_code, png = http_bytes(ns.host, port, "/screen.png")
            if status_code != 200 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                failures.append("qemu backend /screen.png did not return a PNG")
                qemu_screen_digest = None
            else:
                qemu_screen_digest = hashlib.sha256(png).hexdigest()
                out_path = ns.out_dir / f"{ns.prefix}_qemu_screen.png"
                out_path.write_bytes(png)
                screenshots["qemu_screen"] = {"path": str(out_path), "sha256": qemu_screen_digest}
            screen_status = http_json(ns.host, port, "GET", "/api/status?detail=full")
            interactions.append(
                {
                    "step": "qemu-screen",
                    "status": summarize_status(screen_status),
                    "framebuffer": screen_status.get("framebuffer"),
                    "qemu": screen_status.get("qemu"),
                    "legacy_python_hooks": screen_status.get("legacy_python_hooks"),
                }
            )
            if not looks_like_menu_family(
                screen_status,
                require_calibration=ns.qemu_frontend_input_calibration,
            ):
                framebuffer = (
                    screen_status.get("framebuffer")
                    if isinstance(screen_status.get("framebuffer"), dict)
                    else {}
                )
                failures.append(
                    "qemu main menu resources did not render completely: "
                    f"stage={screen_status.get('frontend_input_calibration_stage')} "
                    f"nonzero={framebuffer.get('nonzero_pixels')} "
                    f"unique={framebuffer.get('unique_pixel_values')}"
                )
            try:
                stability = sample_raw_frame_stability(
                    ws,
                    sample_count=ns.frame_stability_samples,
                    frame_timeout=ns.frame_stability_timeout,
                )
                interactions.append(
                    {
                        "step": "qemu-frame-stability",
                        "status": stability,
                    }
                )
                max_change_ratio = float(stability["max_changed_ratio"])
                if max_change_ratio > ns.frame_stability_max_change_ratio:
                    failures.append(
                        "qemu main-menu raw framebuffer was unstable: "
                        f"max_changed_ratio={max_change_ratio:.6f} "
                        f"limit={ns.frame_stability_max_change_ratio:.6f}"
                    )
            except (RuntimeError, ValueError) as exc:
                failures.append(f"qemu raw framebuffer stability check failed: {exc}")
            key_status = http_json(ns.host, port, "POST", "/api/command", {"op": "key", "code": 7, "down": True})
            interactions.append(
                {
                    "step": "qemu-key-bridge",
                    "status": summarize_status(key_status),
                    "qemu_input_result": key_status.get("qemu_input_result"),
                }
            )
            if not key_status.get("input_accepted"):
                failures.append(f"qemu key bridge did not accept input: {key_status.get('qemu_input_result') or key_status.get('warning')}")
            touch_status = http_json(ns.host, port, "POST", "/api/command", {"op": "touch", "x": 150, "y": 220, "down": True})
            touch_drain_status = wait_qemu_display_queue_drained(ns.host, port, 3.0)
            touch_up_status = http_json(ns.host, port, "POST", "/api/command", {"op": "touch", "x": 150, "y": 220, "down": False})
            touch_up_drain_status = wait_qemu_display_queue_drained(ns.host, port, 3.0)
            storage_service_replies = []
            if ns.qemu_run_storage_service:
                for _ in range(3):
                    storage_reply = http_json(
                        ns.host,
                        port,
                        "POST",
                        "/api/command",
                        {"op": "qemu-storage-service", "timeout": 1.0, "max_hits": 256},
                    )
                    storage_service_replies.append(
                        {
                            "status": summarize_status(storage_reply),
                            "result": storage_reply.get("qemu_storage_service_result"),
                        }
                    )
                    result = storage_reply.get("qemu_storage_service_result")
                    if not isinstance(result, dict) or int(result.get("handled_count") or 0) == 0:
                        break
            time.sleep(0.3)
            post_touch_status = http_json(ns.host, port, "GET", "/api/status?detail=full")
            post_touch_framebuffer = (
                post_touch_status.get("framebuffer")
                if isinstance(post_touch_status.get("framebuffer"), dict)
                else {}
            )
            interactions.append(
                {
                    "step": "qemu-touch-bridge",
                    "status": summarize_status(touch_status),
                    "qemu_input_result": touch_status.get("qemu_input_result"),
                    "touch_drain_status": summarize_status(touch_drain_status),
                    "touch_drain_display_event_queue": touch_drain_status.get("display_event_queue"),
                    "touch_up_status": summarize_status(touch_up_status),
                    "touch_up_qemu_input_result": touch_up_status.get("qemu_input_result"),
                    "touch_up_drain_status": summarize_status(touch_up_drain_status),
                    "touch_up_drain_display_event_queue": touch_up_drain_status.get("display_event_queue"),
                    "post_touch_status": summarize_status(post_touch_status),
                    "post_touch_guest_gui_state": post_touch_status.get("guest_gui_state"),
                    "post_touch_display_event_queue": post_touch_status.get("display_event_queue"),
                    "storage_service_replies": storage_service_replies,
                }
            )
            if not looks_like_rendered_screen(post_touch_status):
                failures.append(
                    "qemu screen did not remain rendered after touch: "
                    f"nonzero={post_touch_framebuffer.get('nonzero_pixels')} "
                    f"unique={post_touch_framebuffer.get('unique_pixel_values')}"
                )
            if not touch_status.get("input_accepted"):
                failures.append(f"qemu touch bridge did not accept input: {touch_status.get('qemu_input_result') or touch_status.get('warning')}")
            else:
                qemu_c_touch_consumed = False
                touch_result = touch_status.get("qemu_input_result") if isinstance(touch_status.get("qemu_input_result"), dict) else {}
                touch_up_result = touch_up_status.get("qemu_input_result") if isinstance(touch_up_status.get("qemu_input_result"), dict) else {}
                if touch_result.get("source") == "qemu-c-machine-chardev":
                    if not qemu_display_queue_drained(touch_drain_status):
                        failures.append(f"qemu touch event was not naturally consumed by firmware GUI ring: {touch_drain_status.get('display_event_queue')}")
                    if not qemu_display_queue_drained(touch_up_drain_status):
                        failures.append(f"qemu touch release was not naturally consumed by firmware GUI ring: {touch_up_drain_status.get('display_event_queue')}")
                    qemu_c_touch_consumed = (
                        qemu_display_queue_drained(touch_drain_status)
                        and qemu_display_queue_drained(touch_up_drain_status)
                    )
                else:
                    qemu_c_touch_consumed = False
                    if not isinstance(touch_result.get("gui_handler"), dict):
                        failures.append("qemu touch bridge did not report GUI handler status")
                    else:
                        gui_handler = touch_result.get("gui_handler")
                        assert isinstance(gui_handler, dict)
                        call = gui_handler.get("call") if isinstance(gui_handler.get("call"), dict) else {}
                        if not gui_handler.get("called") or not call.get("returned"):
                            failures.append(f"qemu touch bridge did not return from GUI handler: {gui_handler}")
                    gui_ring_pump = touch_result.get("gui_ring_pump")
                    if not isinstance(gui_ring_pump, dict) or not gui_ring_pump.get("pumped") or not gui_ring_pump.get("called"):
                        failures.append(f"qemu touch bridge did not pump GUI ring: {gui_ring_pump}")
                    modal_close = touch_up_result.get("gui_modal_close_settle") if isinstance(touch_up_result, dict) else None
                    if not isinstance(modal_close, dict) or not modal_close.get("attempted") or not modal_close.get("closed"):
                        failures.append(f"qemu touch release did not settle modal close: {modal_close}")
                    event_poller = touch_up_result.get("gui_event_poller") if isinstance(touch_up_result, dict) else None
                    if not isinstance(event_poller, dict) or not event_poller.get("drained"):
                        failures.append(f"qemu touch release did not drain GUI event flags: {event_poller}")
                    repaint_settle = touch_up_result.get("gui_repaint_settle") if isinstance(touch_up_result, dict) else None
                    if not isinstance(repaint_settle, dict) or not repaint_settle.get("settled"):
                        failures.append(f"qemu touch release did not settle GUI repaint loop: {repaint_settle}")
            status_code, after_png = http_bytes(ns.host, port, "/screen.png")
            if status_code == 200 and after_png.startswith(b"\x89PNG\r\n\x1a\n") and qemu_screen_digest:
                after_digest = hashlib.sha256(after_png).hexdigest()
                screenshots["qemu_screen_after_touch"] = {
                    "path": str(ns.out_dir / f"{ns.prefix}_qemu_screen_after_touch.png"),
                    "sha256": after_digest,
                }
                (ns.out_dir / f"{ns.prefix}_qemu_screen_after_touch.png").write_bytes(after_png)
                if after_digest == qemu_screen_digest and not qemu_c_touch_consumed:
                    failures.append("qemu touch input was accepted but framebuffer did not change")
            stopped = http_json(ns.host, port, "POST", "/api/command", {"op": "stop"})
            interactions.append({"step": "qemu-stop", "status": summarize_status(stopped)})
            if stopped.get("running"):
                failures.append("qemu backend stop left process running")
            logs = http_json(ns.host, port, "GET", "/api/logs?limit=80")
            return _write_summary(ns, port, start, menu_elapsed_seconds, failures, screenshots, interactions, logs, ws)

        auto_started = time.time()
        calibration_reply = ws.send_command({"op": "frontend-input-calibration", "enabled": True}, timeout=3)
        interactions.append(
            {
                "step": "enable-frontend-input-calibration",
                "elapsed_seconds": round(time.time() - auto_started, 3),
                "status": summarize_status(calibration_reply or ws.last_status),
            }
        )

        ws.send_json({"op": "run-start", "name": "web-human-smoke", "steps": 0, "chunk": ns.chunk_steps})
        menu_status = ws.wait_for(
            looks_like_menu,
            ns.boot_timeout,
            poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"),
        )
        menu_elapsed_seconds = time.time() - start
        interactions.append(
            {
                "step": "wait-menu",
                "elapsed_seconds": round(menu_elapsed_seconds, 3),
                "status": summarize_status(menu_status),
                "frames": ws.frames,
            }
        )
        boot_ok = looks_like_menu(menu_status)
        if not boot_ok:
            failures.append("cold boot did not reach a menu-looking framebuffer through the web worker")
            ws.send_json({"op": "stop"})
            ws.pump(1.0)
            stopped = http_json(ns.host, port, "GET", "/api/status")
            interactions.append({"step": "stop-after-boot-failure", "status": summarize_status(stopped)})
            logs = http_json(ns.host, port, "GET", "/api/logs?limit=80")
        else:
            status_code, menu_png, menu_digest = current_ws_screen_digest(ws)
            home_digests = {menu_digest} if menu_digest else set()
            if status_code == 200:
                digest = write_png(ns.out_dir / f"{ns.prefix}_menu.png", menu_png)
                screenshots["menu"] = {"path": str(ns.out_dir / f"{ns.prefix}_menu.png"), "sha256": digest}

            category_points = [
                ("exam", 24, 286),
                ("recite", 72, 286),
                ("dictionary", 120, 286),
                ("entertainment", 168, 286),
                ("tools", 210, 287),
            ]
            home_points = [(38, 306), (202, 306)]
            changed_categories = 0
            poll_status = lambda: http_json(ns.host, port, "GET", "/api/status")
            for name, x, y in category_points:
                step_started = time.time()
                before_seq = ws.last_frame_seq
                tap(ws, x, y, poll_status=poll_status)
                frame_advanced = ws.wait_for_frame_after(before_seq, ns.interaction_frame_timeout)
                status_code, png, digest = current_ws_screen_digest(ws)
                changed = bool(digest and digest != menu_digest)
                changed_categories += 1 if changed or frame_advanced else 0
                out_path = ns.out_dir / f"{ns.prefix}_tap_{name}.png"
                if status_code == 200:
                    out_path.write_bytes(png)
                    screenshots[f"tap_{name}"] = {"path": str(out_path), "sha256": digest}
                tap_status = http_json(ns.host, port, "GET", "/api/status")
                interactions.append(
                    {
                        "step": f"tap-{name}",
                        "elapsed_seconds": round(time.time() - step_started, 3),
                        "display": [x, y],
                        "frame_advanced": frame_advanced,
                        "frame_seq": ws.last_frame_seq,
                        "changed_from_menu": changed,
                        "png": str(out_path) if status_code == 200 else None,
                        "sha256": digest,
                        "status": summarize_status(tap_status),
                    }
                )
                if not changed and not frame_advanced:
                    interactions.append(
                        {
                            "step": f"tap-{name}-unchanged",
                            "note": "category tap did not produce a new WS frame or distinct menu baseline digest",
                            "status": summarize_status(http_json(ns.host, port, "GET", "/api/status")),
                        }
                    )
                    continue
                returned_home = False
                home_digest = ""
                for index, (home_x, home_y) in enumerate(home_points, 1):
                    home_started = time.time()
                    before_seq = ws.last_frame_seq
                    tap(ws, home_x, home_y, poll_status=poll_status)
                    home_status = http_json(ns.host, port, "GET", "/api/status")
                    home_like = looks_like_menu_family(
                        home_status,
                        require_calibration=ns.qemu_frontend_input_calibration,
                    )
                    frame_advanced = False
                    if not home_like:
                        frame_advanced = ws.wait_for_frame_after(before_seq, ns.interaction_frame_timeout)
                        home_status = http_json(ns.host, port, "GET", "/api/status")
                        home_like = looks_like_menu_family(
                            home_status,
                            require_calibration=ns.qemu_frontend_input_calibration,
                        )
                    _home_status, _home_png, home_digest = current_ws_screen_digest(ws)
                    returned = bool(home_digest and home_digest in home_digests) or home_like
                    interactions.append(
                        {
                            "step": f"home-after-{name}-{index}",
                            "elapsed_seconds": round(time.time() - home_started, 3),
                            "display": [home_x, home_y],
                            "frame_advanced": frame_advanced,
                            "frame_seq": ws.last_frame_seq,
                            "returned_to_menu": returned,
                            "sha256": home_digest,
                            "status": summarize_status(home_status),
                        }
                    )
                    if returned:
                        if home_digest:
                            home_digests.add(home_digest)
                        returned_home = True
                        break
                if not returned_home:
                    cancel_started = time.time()
                    before_seq = ws.last_frame_seq
                    key_press(ws, 9, poll_status=poll_status)
                    cancel_status = http_json(ns.host, port, "GET", "/api/status")
                    frame_advanced = False
                    if not looks_like_menu_family(
                        cancel_status,
                        require_calibration=ns.qemu_frontend_input_calibration,
                    ):
                        frame_advanced = ws.wait_for_frame_after(before_seq, ns.interaction_frame_timeout)
                        cancel_status = http_json(ns.host, port, "GET", "/api/status")
                    _home_status, _home_png, home_digest = current_ws_screen_digest(ws)
                    returned = bool(
                        home_digest and home_digest in home_digests
                    ) or looks_like_menu_family(
                        cancel_status,
                        require_calibration=ns.qemu_frontend_input_calibration,
                    )
                    interactions.append(
                        {
                            "step": f"cancel-after-{name}",
                            "elapsed_seconds": round(time.time() - cancel_started, 3),
                            "frame_advanced": frame_advanced,
                            "frame_seq": ws.last_frame_seq,
                            "returned_to_menu": returned,
                            "sha256": home_digest,
                            "status": summarize_status(cancel_status),
                        }
                    )
                    if returned and home_digest:
                        home_digests.add(home_digest)
                    returned_home = returned
                if not returned_home:
                    interactions.append(
                        {
                            "step": f"return-after-{name}-not-menu-like",
                            "note": "continuing category coverage from current screen",
                            "status": summarize_status(http_json(ns.host, port, "GET", "/api/status")),
                        }
                    )
            if changed_categories == 0:
                failures.append("bottom category taps did not advance framebuffer frames")

            for code, name in [(4, "up"), (5, "down"), (6, "left"), (7, "right"), (9, "cancel"), (10, "ok")]:
                key_press(ws, code, poll_status=poll_status)
                status = ws.wait_for(
                    lambda s: int(s.get("pending_keys") or 0) == 0,
                    8,
                    poll_status=poll_status,
                )
                interactions.append({"step": f"key-{name}", "code": code, "status": summarize_status(status)})
                if int(status.get("pending_keys") or 0) != 0:
                    failures.append(f"key {name} left pending_keys={status.get('pending_keys')}")

            stop_seq = int(time.time() * 1000)
            ws.send_json({"op": "stop", "command_seq": stop_seq})
            stop_reply = ws.wait_for_command_seq(stop_seq, 5)
            stopped = stop_reply or ws.last_status
            if stop_reply is None or stopped.get("running"):
                interactions.append(
                    {
                        "step": "ws-stop-timeout",
                        "command_seq": stop_seq,
                        "reply_seen": stop_reply is not None,
                        "status": summarize_status(stopped),
                    }
                )
                stopped = http_json(ns.host, port, "POST", "/api/command", {"op": "stop"})
                deadline = time.time() + 5
                while stopped.get("running") and time.time() < deadline:
                    time.sleep(0.2)
                    stopped = http_json(ns.host, port, "GET", "/api/status")
            interactions.append({"step": "stop", "status": summarize_status(stopped)})
            if stopped.get("running"):
                failures.append("stop command left frontend running")

            logs = http_json(ns.host, port, "GET", "/api/logs?limit=80")
    finally:
        if ws is not None:
            ws.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        elapsed = time.time() - start

    summary = {
        "ok": not failures,
        "host": ns.host,
        "port": port,
        "used_existing": ns.use_existing,
        "elapsed_seconds": round(elapsed, 3),
        "menu_elapsed_seconds": None if menu_elapsed_seconds is None else round(menu_elapsed_seconds, 3),
        "failures": failures,
        "screenshots": screenshots,
        "last_frame_wire_kind": None if ws is None else ws.last_frame_wire_kind,
        "interactions": interactions,
        "log_count": logs.get("count"),
        "recent_logs": logs.get("events", []),
    }
    json_path = ns.out_dir / f"{ns.prefix}_summary.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report_path = ns.out_dir / f"{ns.prefix}_report.md"
    lines = [
        "# Frontend Web Smoke Report",
        "",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'}",
        f"- URL: http://{ns.host}:{port}/",
        f"- Elapsed seconds: {summary['elapsed_seconds']}",
        f"- Menu elapsed seconds: {summary['menu_elapsed_seconds']}",
        f"- Frames received over WS: {ws.frames if ws is not None else 0}",
        f"- Last WS frame wire kind: {None if ws is None else ws.last_frame_wire_kind}",
        f"- Failures: {len(failures)}",
        "",
        "## Interactions",
    ]
    for item in interactions:
        extras: list[str] = []
        if "elapsed_seconds" in item:
            extras.append(f"elapsed={item['elapsed_seconds']}s")
        if "frame_advanced" in item:
            extras.append(f"frame_advanced={item['frame_advanced']}")
        if "frame_seq" in item:
            extras.append(f"frame_seq={item['frame_seq']}")
        prefix = "" if not extras else f" ({', '.join(extras)})"
        lines.append(f"- {item['step']}{prefix}: `{json.dumps(item.get('status', {}), ensure_ascii=False)}`")
    if failures:
        lines += ["", "## Failures"]
        lines += [f"- {failure}" for failure in failures]
    lines += [
        "",
        "## Notes",
        "- This smoke drives the frontend through HTTP and WebSocket, not direct Python state calls.",
        "- Touches use rendered display coordinates and rely on the frontend orientation mapping.",
        "- Category coverage is raw-frame-sequence based; screenshots are converted only when recorded.",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"ok": summary["ok"], "summary": str(json_path), "report": str(report_path), "failures": failures}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
