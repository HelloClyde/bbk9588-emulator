#!/usr/bin/env python3
"""Drive the Thunder Fighter path through the real web frontend."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from urllib.parse import quote

from emu.tools.inspect_combined_nand_fat import Fat16View, extract_fat_from_nand

from tests.run_frontend_web_smoke import (
    BUILD,
    WebSocketClient,
    fetch_screen_digest,
    find_free_port,
    http_json,
    key_press,
    looks_like_menu,
    start_frontend,
    summarize_status,
    tap,
    wait_http,
)

KEY_RIGHT = 7
KEY_OK = 10
KEY_LEFT = 6
KEY_CANCEL = 9
KEY_DISMISS_PET = KEY_OK


def read_syspet_balance(nand_image: Path) -> int | None:
    try:
        fat = extract_fat_from_nand(nand_image, 0x1C40, 2048, 64)
        view = Fat16View(fat)
        entry = view.find(["\u7cfb\u7edf", "\u6570\u636e", "SysPet.yzj"])
        if entry is None:
            return None
        data = view.read_file(entry)
        if len(data) < 0x2C:
            return None
        return int.from_bytes(data[0x28:0x2C], "little", signed=True)
    except Exception:
        return None


def pump_for(ws: WebSocketClient, seconds: float) -> None:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        ws.recv_one()


def framebuffer_stats(status: dict[str, object]) -> tuple[int, int]:
    fb = status.get("framebuffer") if isinstance(status.get("framebuffer"), dict) else {}
    return int(fb.get("nonzero_pixels") or 0), int(fb.get("unique_pixel_values") or 0)


def thunder_fullscreen_like(status: dict[str, object]) -> bool:
    nonzero, unique = framebuffer_stats(status)
    return nonzero >= 70000 and unique >= 500


def png_rgb_stats(png: bytes) -> dict[str, int]:
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return {}
    pos = 8
    width = 0
    height = 0
    idat: list[bytes] = []
    while pos + 8 <= len(png):
        size = struct.unpack(">I", png[pos : pos + 4])[0]
        kind = png[pos + 4 : pos + 8]
        payload = png[pos + 8 : pos + 8 + size]
        pos += 12 + size
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB",
                payload,
            )
            if bit_depth != 8 or color_type != 2 or compression != 0 or filter_method != 0 or interlace != 0:
                return {}
        elif kind == b"IDAT":
            idat.append(payload)
        elif kind == b"IEND":
            break
    if width <= 0 or height <= 0 or not idat:
        return {}
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return {}
    row_size = width * 3
    yellow = 0
    white = 0
    bluebar = 0
    red = 0
    green = 0
    pos = 0
    for _y in range(height):
        if pos >= len(raw) or raw[pos] != 0:
            return {}
        pos += 1
        row = raw[pos : pos + row_size]
        pos += row_size
        for i in range(0, len(row), 3):
            r, g, b = row[i], row[i + 1], row[i + 2]
            if r > 180 and g > 140 and b < 100:
                yellow += 1
            if r > 210 and g > 210 and b > 210:
                white += 1
            if b > 80 and r < 80 and g < 130:
                bluebar += 1
            if r > 180 and g < 100 and b < 100:
                red += 1
            if g > 150 and r < 120 and b < 120:
                green += 1
    return {
        "yellow": yellow,
        "white": white,
        "bluebar": bluebar,
        "red": red,
        "green": green,
    }


def thunder_menu_capture_like(capture: dict[str, object]) -> bool:
    stats = capture.get("image_stats")
    if not isinstance(stats, dict):
        return False
    return int(stats.get("yellow") or 0) >= 800


def game_grid_capture_like(capture: dict[str, object]) -> bool:
    stats = capture.get("image_stats")
    if not isinstance(stats, dict):
        return False
    return (
        int(stats.get("white") or 0) >= 4500
        and int(stats.get("bluebar") or 0) >= 2500
        and int(stats.get("green") or 0) < 1000
        and int(stats.get("yellow") or 0) < 400
    )


def pet_popup_capture_like(capture: dict[str, object]) -> bool:
    stats = capture.get("image_stats")
    if not isinstance(stats, dict):
        return False
    yellow = int(stats.get("yellow") or 0)
    white = int(stats.get("white") or 0)
    bluebar = int(stats.get("bluebar") or 0)
    green = int(stats.get("green") or 0)
    return (
        yellow >= 700
        and white < 7000
        and (green >= 1000 or bluebar >= 2500)
    )


def save_capture(
    host: str,
    port: int,
    out_dir: Path,
    prefix: str,
    name: str,
) -> dict[str, object]:
    status_code, png, digest = fetch_screen_digest(host, port)
    path = out_dir / f"{prefix}_{name}.png"
    image_stats = png_rgb_stats(png) if status_code == 200 else {}
    if status_code == 200:
        path.write_bytes(png)
    status = http_json(host, port, "GET", "/api/status")
    return {
        "name": name,
        "path": str(path) if status_code == 200 else None,
        "sha256": digest,
        "status_code": status_code,
        "image_stats": image_stats,
        "status": summarize_status(status),
    }


def save_ws_capture(
    ws: WebSocketClient,
    host: str,
    port: int,
    out_dir: Path,
    prefix: str,
    name: str,
) -> dict[str, object]:
    png = ws.current_frame_png()
    if not png:
        return save_capture(host, port, out_dir, prefix, name)
    digest = hashlib.sha256(png).hexdigest()
    path = out_dir / f"{prefix}_{name}.png"
    path.write_bytes(png)
    status = ws.last_status or http_json(host, port, "GET", "/api/status")
    return {
        "name": name,
        "path": str(path),
        "sha256": digest,
        "status_code": 200,
        "image_stats": png_rgb_stats(png),
        "status": summarize_status(status),
    }


def summarize_event_probe(status: dict[str, object]) -> dict[str, object]:
    probe = status.get("native_bda_event_probe")
    input_state = status.get("input")
    out: dict[str, object] = {
        "status": summarize_status(status),
        "native_bda_event_probe": probe,
    }
    if isinstance(input_state, dict):
        out["key_down_codes"] = input_state.get("key_down_codes")
        for key in (
            "key_controller_event_log",
            "touch_controller_event_log",
            "gui_key_event_log",
        ):
            value = input_state.get(key)
            if isinstance(value, list):
                out[f"{key}_tail"] = value[-8:]
    return out


def capture_after(
    ws: WebSocketClient,
    host: str,
    port: int,
    out_dir: Path,
    prefix: str,
    name: str,
    seconds: float,
) -> dict[str, object]:
    pump_for(ws, seconds)
    return save_capture(host, port, out_dir, prefix, name)


def capture_when(
    ws: WebSocketClient,
    host: str,
    port: int,
    out_dir: Path,
    prefix: str,
    name: str,
    *,
    timeout: float,
    min_wait: float = 0.0,
    status_predicate=None,
    capture_predicate=None,
    poll_interval: float = 0.25,
    check_interval: float = 1.0,
) -> dict[str, object]:
    deadline = time.time() + max(0.0, timeout)
    if min_wait > 0:
        pump_for(ws, min_wait)
    last_capture: dict[str, object] | None = None
    last_poll = 0.0
    last_check = 0.0
    while time.time() < deadline:
        ws.recv_one()
        now = time.time()
        if status_predicate is None:
            status = ws.last_status
        elif now - last_poll >= 0.5:
            status = http_json(host, port, "GET", "/api/status")
            ws.last_status = status
            last_poll = now
        else:
            status = ws.last_status
        if status_predicate is not None and not status_predicate(status):
            time.sleep(poll_interval)
            continue
        if capture_predicate is not None and now - last_check < check_interval:
            continue
        last_check = now
        last_capture = save_ws_capture(ws, host, port, out_dir, prefix, name)
        if capture_predicate is None or capture_predicate(last_capture):
            return last_capture
        time.sleep(poll_interval)
    return last_capture or save_capture(host, port, out_dir, prefix, name)


def tap_until_capture_changes(
    ws: WebSocketClient,
    host: str,
    port: int,
    out_dir: Path,
    prefix: str,
    name: str,
    x: int,
    y: int,
    old_digest: str,
    *,
    attempts: int = 3,
    timeout: float = 3.0,
) -> tuple[dict[str, object], bool]:
    last_capture: dict[str, object] = {}
    for attempt in range(max(1, attempts)):
        tap(ws, x, y, poll_status=lambda: http_json(host, port, "GET", "/api/status"))
        capture_name = name if attempt == 0 else f"{name}_retry{attempt}"
        last_capture = capture_when(
            ws,
            host,
            port,
            out_dir,
            prefix,
            capture_name,
            timeout=timeout,
            min_wait=0.3,
            capture_predicate=lambda capture, old=old_digest: str(capture.get("sha256") or "") != old,
            check_interval=0.25,
        )
        if str(last_capture.get("sha256") or "") != old_digest:
            return last_capture, True
    return last_capture, False


def tap_until_capture_predicate(
    ws: WebSocketClient,
    host: str,
    port: int,
    out_dir: Path,
    prefix: str,
    name: str,
    x: int,
    y: int,
    predicate,
    *,
    attempts: int = 4,
    timeout: float = 3.0,
) -> tuple[dict[str, object], bool]:
    last_capture: dict[str, object] = {}
    poll_status = lambda: http_json(host, port, "GET", "/api/status")
    for attempt in range(max(1, attempts)):
        tap(ws, x, y, poll_status=poll_status)
        capture_name = name if attempt == 0 else f"{name}_retry{attempt}"
        last_capture = capture_when(
            ws,
            host,
            port,
            out_dir,
            prefix,
            capture_name,
            timeout=timeout,
            min_wait=0.3,
            capture_predicate=lambda capture: predicate(capture) or pet_popup_capture_like(capture),
            check_interval=0.25,
        )
        if predicate(last_capture):
            return last_capture, True
        if pet_popup_capture_like(last_capture):
            key_press(ws, KEY_DISMISS_PET, poll_status=poll_status)
            pump_for(ws, 0.8)
    return last_capture, False


def make_contact_sheet(captures: list[dict[str, object]], out_path: Path) -> str | None:
    image_paths = [Path(str(item["path"])) for item in captures if item.get("path")]
    image_paths = [path for path in image_paths if path.exists()]
    if not image_paths:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        cols = min(4, len(image_paths))
        layout = "|".join(f"{(idx % cols) * 240}_{(idx // cols) * 320}" for idx in range(len(image_paths)))
        cmd = [ffmpeg, "-y"]
        for image_path in image_paths:
            cmd += ["-i", str(image_path)]
        cmd += ["-filter_complex", f"xstack=inputs={len(image_paths)}:layout={layout}:fill=black", "-frames:v", "1", str(out_path)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except Exception:
            return None
        return str(out_path)

    tiles = []
    for capture in captures:
        path = capture.get("path")
        if not path:
            continue
        image_path = Path(str(path))
        if image_path.exists():
            tiles.append((str(capture.get("name") or image_path.stem), Image.open(image_path).convert("RGB")))
    if not tiles:
        return None
    tile_w, tile_h = 240, 344
    cols = min(4, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w, rows * tile_h), "black")
    draw = ImageDraw.Draw(sheet)
    for idx, (name, image) in enumerate(tiles):
        x = (idx % cols) * tile_w
        y = (idx // cols) * tile_h
        sheet.paste(image, (x, y))
        draw.text((x + 4, y + 322), name, fill="white")
    sheet.save(out_path)
    return str(out_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a real-system web smoke for Thunder Fighter.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="Use 0 to start a private frontend.")
    ap.add_argument("--use-existing", action="store_true")
    ap.add_argument("--nand-image", type=Path, default=None, help="Override app.py's default NAND image.")
    ap.add_argument("--out-dir", type=Path, default=BUILD)
    ap.add_argument("--prefix", default="thunder_web_smoke")
    ap.add_argument("--boot-mode", choices=["nand", "c200", "uboot"], default="nand")
    ap.add_argument("--boot-timeout", type=int, default=240)
    ap.add_argument("--runtime-timeout", type=int, default=45)
    ap.add_argument("--chunk-steps", type=int, default=250000)
    ap.add_argument("--frame-push-min-interval", type=float, default=0.04)
    ap.add_argument("--fps-probe-seconds", type=float, default=0.0)
    ap.add_argument("--frontend-profile-out", type=Path)
    ap.add_argument("--event-probe", action="store_true", default=False)
    ap.add_argument("--battle-state-out", type=Path, help="Save a frontend checkpoint after Thunder reaches battle.")
    ap.add_argument("--backend", choices=["qemu"], default="qemu")
    ap.add_argument("--qemu", default=None)
    ap.add_argument("--qemu-machine", dest="qemu_machine", default=None)
    ap.add_argument("--qemu-cpu", dest="qemu_cpu", default=None)
    ap.add_argument("--qemu-accel", dest="qemu_accel", default=None)
    ap.add_argument("--qemu-gdb", dest="qemu_gdb", default=None)
    ap.add_argument("--qemu-timeout", dest="qemu_timeout", type=float, default=None)
    ap.add_argument("--qemu-firmware-patch", action="append", default=None)
    ap.add_argument("--qemu-extra-arg", action="append", default=[])
    ns = ap.parse_args(argv)

    ns.out_dir.mkdir(parents=True, exist_ok=True)
    port = ns.port or find_free_port(ns.host)
    proc: subprocess.Popen[bytes] | None = None
    ws: WebSocketClient | None = None
    captures: list[dict[str, object]] = []
    interactions: list[dict[str, object]] = []
    failures: list[str] = []
    balance = None if ns.use_existing else read_syspet_balance(ns.nand_image)
    start = time.time()
    logs: dict[str, object] = {}

    try:
        if not ns.use_existing:
            proc = start_frontend(ns, port)
        wait_http(ns.host, port, 30)
        ws = WebSocketClient(ns.host, port)
        pump_for(ws, 1.0)

        ws.send_json({"op": "reset"})
        pump_for(ws, 1.0)
        ws.send_json({"op": "frontend-input-calibration", "enabled": True})
        if ns.backend == "qemu":
            resource_trace_status = ws.send_command(
                {"op": "qemu-resource-trace", "timeout": min(20.0, ns.boot_timeout), "max_hits": 2048},
                timeout=min(25.0, ns.boot_timeout + 5.0),
            ) or {}
            interactions.append(
                {
                    "step": "qemu-resource-trace-after-frontend-input-calibration",
                    "result": resource_trace_status.get("qemu_resource_trace_result"),
                    "qemu": resource_trace_status.get("qemu"),
                }
            )
            deadline = time.time() + ns.boot_timeout
            active_status: dict[str, object] | None = None
            last_status: dict[str, object] = {}
            while time.time() < deadline:
                last_status = http_json(ns.host, port, "GET", "/api/status?detail=full")
                gui = last_status.get("guest_gui_state") if isinstance(last_status.get("guest_gui_state"), dict) else {}
                if last_status.get("frontend_input_calibration_stage") == 12 and gui.get("active_object_ready"):
                    active_status = last_status
                    break
                pump_for(ws, 0.2)
            interactions.append(
                {
                    "step": "qemu-wait-active-object",
                    "status": summarize_status(active_status or last_status),
                    "guest_display_surface": (active_status or last_status).get("guest_display_surface"),
                    "guest_gui_state": (active_status or last_status).get("guest_gui_state"),
                    "qemu_frontend_input_calibration_log": (active_status or last_status).get("qemu_frontend_input_calibration_log"),
                    "legacy_python_hooks": (active_status or last_status).get("legacy_python_hooks"),
                }
            )
            if active_status is None:
                failures.append("qemu frontend input calibration did not reach an active GUI object")
            else:
                modal_capture = capture_after(
                    ws,
                    ns.host,
                    port,
                    ns.out_dir,
                    ns.prefix,
                    "00_qemu_initial_screen",
                    0.5,
                )
                captures.append(modal_capture)
                modal_status = http_json(ns.host, port, "GET", "/api/status?detail=full")
                gui = modal_status.get("guest_gui_state") if isinstance(modal_status.get("guest_gui_state"), dict) else {}
                if gui.get("modal_804a65c0"):
                    failures.append("qemu initial system modal is visible; system files were not read correctly")
                interactions.append(
                    {
                        "step": "qemu-initial-screen",
                        "capture": modal_capture,
                        "status": modal_capture.get("status"),
                        "guest_gui_state": gui,
                    }
                )
        pump_for(ws, 0.5)
        ws.send_json({"op": "run-start", "name": "thunder-web-smoke", "steps": 0, "chunk": ns.chunk_steps})
        menu_status = ws.wait_for(
            looks_like_menu,
            ns.boot_timeout,
            poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"),
        )
        interactions.append({"step": "wait-menu", "status": summarize_status(menu_status)})
        if not looks_like_menu(menu_status):
            if ns.backend == "qemu":
                screen_status_code, screen_png, screen_digest = fetch_screen_digest(ns.host, port)
                if screen_status_code == 200:
                    screen_path = ns.out_dir / f"{ns.prefix}_qemu_wait_menu.png"
                    screen_path.write_bytes(screen_png)
                qemu_menu_status = http_json(ns.host, port, "GET", "/api/status?detail=full")
                interactions.append(
                    {
                        "step": "qemu-wait-menu-detail",
                        "status": summarize_status(qemu_menu_status),
                        "screen_status_code": screen_status_code,
                        "screen_sha256": screen_digest,
                        "screen_path": str(screen_path) if screen_status_code == 200 else None,
                        "screen_image_stats": png_rgb_stats(screen_png) if screen_status_code == 200 else {},
                        "framebuffer": qemu_menu_status.get("framebuffer"),
                        "guest_display_surface": qemu_menu_status.get("guest_display_surface"),
                        "guest_gui_state": qemu_menu_status.get("guest_gui_state"),
                    }
                )
            failures.append("cold boot did not reach the main menu")
        else:
            menu_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "00_menu", 0.5)
            captures.append(menu_capture)

            poll_status = lambda: http_json(ns.host, port, "GET", "/api/status")
            if pet_popup_capture_like(menu_capture):
                key_press(ws, KEY_DISMISS_PET, poll_status=poll_status)
                menu_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "00_menu_dismiss_pet", 1.0)
                captures.append(menu_capture)
            tap(ws, 168, 286, poll_status=poll_status)
            entertainment_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "01_entertainment", 1.0)
            if pet_popup_capture_like(entertainment_capture):
                key_press(ws, KEY_DISMISS_PET, poll_status=poll_status)
                entertainment_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "01_entertainment_dismiss_pet", 1.0)
                tap(ws, 168, 286, poll_status=poll_status)
                entertainment_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "01_entertainment_retry", 1.0)
            captures.append(entertainment_capture)

            grid_capture = entertainment_capture
            grid_opened = False
            for index in range(1, 5):
                tap(ws, 198, 84, poll_status=poll_status)
                grid_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, f"02_game_grid_r{index}", 0.8)
                captures.append(grid_capture)
                if game_grid_capture_like(grid_capture):
                    grid_opened = True
            if grid_opened:
                # Keep the default-image path aligned with the known-good saved
                # checkpoint route. Earlier pages can look grid-like but keep a
                # different launcher focus, which opens the wrong app.
                grid_opened = game_grid_capture_like(grid_capture)
            captures.append(grid_capture)
            grid_status = http_json(ns.host, port, "GET", "/api/status")
            interactions.append(
                {
                    "step": "open-entertainment-world",
                    "digest_changed": grid_opened,
                    "status": summarize_status(grid_status),
                }
            )
            thunder_selected = False
            if not grid_opened:
                failures.append("Entertainment game grid did not open before Entertainment World selection")
            else:
                # In the app.py default NAND image Thunder is under:
                # 娱乐 tab -> game grid -> 娱乐天地 -> 雷霆战机.
                key_press(ws, KEY_LEFT, poll_status=poll_status)
                captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "03_select_ent_world_left", 0.8))
                tap(ws, 120, 72, poll_status=poll_status)
                captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "04_tap_ent_world", 1.5))
                tap(ws, 150, 306, poll_status=poll_status)
                captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "05_bottom_open_ent_world", 1.5))
                key_press(ws, KEY_RIGHT, poll_status=poll_status)
                captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "06_ent_world_right", 0.4))
                key_press(ws, KEY_OK, poll_status=poll_status)
                captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "07_ent_world_ok", 2.0))
                key_press(ws, KEY_LEFT, poll_status=poll_status)
                world_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "08_ent_world_grid", 0.4)
                captures.append(world_capture)
                thunder_world_opened = str(world_capture.get("sha256") or "") != str(grid_capture.get("sha256") or "")
                interactions.append(
                    {
                        "step": "open-entertainment-world-list",
                        "digest_changed": thunder_world_opened,
                        "status": summarize_status(http_json(ns.host, port, "GET", "/api/status")),
                    }
                )
                if not thunder_world_opened:
                    failures.append("Entertainment World list did not open before Thunder selection")
                else:
                    select_capture, selected_thunder = tap_until_capture_changes(
                        ws,
                        ns.host,
                        port,
                        ns.out_dir,
                        ns.prefix,
                        "09_select_thunder",
                        135,
                        260,
                        str(world_capture.get("sha256") or ""),
                        attempts=2,
                        timeout=2.0,
                    )
                    captures.append(select_capture)
                    if not selected_thunder:
                        failures.append("Thunder icon selection did not change the Entertainment World grid")
                    thunder_selected = selected_thunder
                    tap(ws, 135, 260, poll_status=poll_status)
                    captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "10_open_thunder_touch", 2.0))
                    tap(ws, 150, 306, poll_status=poll_status)
                    captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "11_open_thunder_toolbar", 2.0))

            if thunder_selected:
                key_press(ws, KEY_OK, poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"))
                captures.append(capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, "04_loading_or_payment", 3.0))
                entry_status = http_json(ns.host, port, "GET", "/api/status")
                interactions.append({"step": "open-thunder-entry", "status": summarize_status(entry_status)})

                if balance is not None and balance < 2:
                    failures.append(f"SysPet.yzj balance is {balance}, expected at least 2")

                # If the billing dialog is shown, this hits its confirm button.
                # If the game has already moved to loading, the tap is harmless.
                tap(ws, 82, 202, poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"))
                menu_capture = capture_when(
                    ws,
                    ns.host,
                    port,
                    ns.out_dir,
                    ns.prefix,
                    "05_thunder_menu",
                    timeout=min(float(ns.runtime_timeout), 20.0),
                    min_wait=0.5,
                    status_predicate=thunder_fullscreen_like,
                    capture_predicate=thunder_menu_capture_like,
                )
                captures.append(menu_capture)
                wait_menu_index = 0
                deadline = time.time() + ns.runtime_timeout
                while not thunder_menu_capture_like(menu_capture) and time.time() < deadline:
                    menu_capture = capture_after(
                        ws,
                        ns.host,
                        port,
                        ns.out_dir,
                        ns.prefix,
                        f"05_wait_thunder_menu_{wait_menu_index:02d}",
                        3.0,
                    )
                    captures.append(menu_capture)
                    wait_menu_index += 1
                menu_status = http_json(ns.host, port, "GET", "/api/status")
                interactions.append({"step": "thunder-menu", "status": summarize_status(menu_status)})
                if not thunder_fullscreen_like(menu_status) or not thunder_menu_capture_like(menu_capture):
                    failures.append("Thunder did not reach its actionable menu after billing/loading")

                menu_digest = str(menu_capture.get("sha256") or "")
                thunder_menu_ready = thunder_fullscreen_like(menu_status) and thunder_menu_capture_like(menu_capture)

                if thunder_menu_ready:
                    key_press(ws, KEY_OK, poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"))
                    start_capture = capture_when(
                        ws,
                        ns.host,
                        port,
                        ns.out_dir,
                        ns.prefix,
                        "06_after_start_ok",
                        timeout=8.0,
                        min_wait=0.3,
                        capture_predicate=lambda capture, old=menu_digest: capture.get("sha256") != old,
                    )
                    captures.append(start_capture)
                    if start_capture.get("sha256") == menu_digest:
                        tap(ws, 105, 142, poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"))
                        start_capture = capture_when(
                            ws,
                            ns.host,
                            port,
                            ns.out_dir,
                            ns.prefix,
                            "06_after_start_tap",
                            timeout=8.0,
                            min_wait=0.3,
                            capture_predicate=lambda capture, old=menu_digest: capture.get("sha256") != old,
                        )
                        captures.append(start_capture)
                    start_status = http_json(ns.host, port, "GET", "/api/status")
                    interactions.append({"step": "start-game", "digest_changed": start_capture.get("sha256") != menu_digest, "status": summarize_status(start_status)})
                    if start_capture.get("sha256") == menu_digest:
                        failures.append("Thunder menu did not react to game-start input")

                    before_battle_digest = str(start_capture.get("sha256") or "")
                    if ns.event_probe:
                        interactions.append(
                            {
                                "step": "event-probe-before-plane-ok",
                                **summarize_event_probe(http_json(ns.host, port, "GET", "/api/status?detail=full")),
                            }
                        )
                    key_press(ws, KEY_OK, poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"))
                    if ns.event_probe:
                        interactions.append(
                            {
                                "step": "event-probe-after-plane-ok",
                                **summarize_event_probe(http_json(ns.host, port, "GET", "/api/status?detail=full")),
                            }
                        )
                    battle_capture = capture_when(
                        ws,
                        ns.host,
                        port,
                        ns.out_dir,
                        ns.prefix,
                        "07_after_plane_ok",
                        timeout=12.0,
                        min_wait=0.3,
                        capture_predicate=lambda capture, old=before_battle_digest: capture.get("sha256") != old,
                    )
                    captures.append(battle_capture)
                    deadline = time.time() + ns.runtime_timeout
                    wait_index = 0
                    while battle_capture.get("sha256") == before_battle_digest and time.time() < deadline:
                        battle_capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, f"07_wait_battle_{wait_index:02d}", 3.0)
                        captures.append(battle_capture)
                        wait_index += 1
                    battle_status = http_json(ns.host, port, "GET", "/api/status")
                    interactions.append({"step": "enter-battle", "digest_changed": battle_capture.get("sha256") != before_battle_digest, "status": summarize_status(battle_status)})
                    if battle_capture.get("sha256") == before_battle_digest:
                        failures.append("Thunder did not advance from plane selection to a battle-looking screen")
                    elif ns.battle_state_out is not None:
                        checkpoint_status = http_json(
                            ns.host,
                            port,
                            "POST",
                            f"/api/checkpoint?path={quote(str(ns.battle_state_out))}",
                        )
                        interactions.append(
                            {
                                "step": "save-battle-checkpoint",
                                "path": str(ns.battle_state_out),
                                "status": summarize_status(checkpoint_status),
                            }
                        )

                    if ns.fps_probe_seconds > 0 and ws is not None:
                        probe_start_frames = ws.frames
                        probe_start = time.time()
                        probe_deadline = probe_start + float(ns.fps_probe_seconds)
                        while time.time() < probe_deadline:
                            ws.recv_one()
                        probe_elapsed = max(0.001, time.time() - probe_start)
                        probe_status = http_json(ns.host, port, "GET", "/api/status")
                        interactions.append(
                            {
                                "step": "battle-fps-probe",
                                "seconds": round(probe_elapsed, 3),
                                "frames_delta": ws.frames - probe_start_frames,
                                "frames_per_second": (ws.frames - probe_start_frames) / probe_elapsed,
                                "status": summarize_status(probe_status),
                            }
                        )

                    before_input_digest = str(battle_capture.get("sha256") or before_battle_digest)
                    for name, code in (("left", KEY_LEFT), ("right", KEY_RIGHT), ("ok", KEY_OK)):
                        key_press(ws, code, poll_status=lambda: http_json(ns.host, port, "GET", "/api/status"))
                        capture = capture_after(ws, ns.host, port, ns.out_dir, ns.prefix, f"08_input_{name}", 2.0)
                        captures.append(capture)
                        interactions.append({
                            "step": f"input-{name}",
                            "digest_changed": capture.get("sha256") != before_input_digest,
                            "status": capture.get("status"),
                        })
                        before_input_digest = str(capture.get("sha256") or before_input_digest)

        if ws is not None:
            ws.send_json({"op": "stop"})
            pump_for(ws, 1.0)
        logs = http_json(ns.host, port, "GET", "/api/logs?limit=160")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
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
    contact_sheet = make_contact_sheet(captures, ns.out_dir / f"{ns.prefix}_contactsheet.png")
    summary = {
        "ok": not failures,
        "host": ns.host,
        "port": port,
        "used_existing": ns.use_existing,
        "nand_image": str(ns.nand_image),
        "syspet_balance": balance,
        "elapsed_seconds": round(elapsed, 3),
        "failures": failures,
        "captures": captures,
        "contact_sheet": contact_sheet,
        "interactions": interactions,
        "log_count": logs.get("count") if isinstance(logs, dict) else None,
        "recent_logs": logs.get("events", []) if isinstance(logs, dict) else [],
    }
    json_path = ns.out_dir / f"{ns.prefix}_summary.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report_path = ns.out_dir / f"{ns.prefix}_report.md"
    lines = [
        "# Thunder Web Smoke Report",
        "",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'}",
        f"- Elapsed seconds: {summary['elapsed_seconds']}",
        f"- SysPet balance: {balance}",
        f"- Contact sheet: {contact_sheet}",
        "",
        "## Steps",
    ]
    for item in interactions:
        lines.append(f"- {item['step']}: `{json.dumps(item, ensure_ascii=False)}`")
    if failures:
        lines += ["", "## Failures"]
        lines += [f"- {failure}" for failure in failures]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "summary": str(json_path), "report": str(report_path), "contact_sheet": contact_sheet}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
