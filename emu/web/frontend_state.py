"""QEMU-backed frontend state for the BBK 9588 web UI."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import threading
import time
from collections import deque
from itertools import islice
from pathlib import Path, PurePosixPath

from emu.core.framebuffer import png_bytes_from_rgb, rgb565_raw_to_info_rgb
from emu.qemu.nand_fs import (
    extract_logical_fat_image,
    join_nand_path,
    list_fat_directory,
    mutate_nand_files,
    normalize_nand_path,
    read_fat_file,
    validate_nand_image,
)
from emu.qemu.nand_lock import NandImageLease
from emu.qemu.nand_source import import_nand_source
from emu.qemu.system import (
    DEFAULT_QEMU_EXECUTABLE,
    DEFAULT_QEMU_MACHINE,
    DEFAULT_QEMU_NAND_IMAGE,
    TOUCH_CALIBRATION_REFERENCE_POINTS,
    QemuProcessBackend,
    build_bbk_qemu_config,
    classify_guest_pc,
    migrate_legacy_nand_checkpoint,
)

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
COMBINED_NAND_IMAGE_CANDIDATES = (
    ROOT / DEFAULT_QEMU_NAND_IMAGE,
)
NAND_IMAGE_GLOB_PATTERNS = (
    "bbk9588_nand*.bin",
    "bbk9588_nand*.img",
)

FRONTEND_INPUT_DIALOG_X = 150
FRONTEND_INPUT_DIALOG_Y = 205
FRONTEND_INPUT_CALIBRATION_TARGETS = tuple((x, y) for x, y, _raw_x, _raw_y in TOUCH_CALIBRATION_REFERENCE_POINTS)
FRONTEND_INPUT_CALIBRATION_STAGE_LABELS = {
    0: "calib-1-down",
    1: "calib-1-up",
    2: "calib-2-down",
    3: "calib-2-up",
    4: "calib-3-down",
    5: "calib-3-up",
    6: "calib-4-down",
    7: "calib-4-up",
    8: "wait-dialog",
    9: "dialog-wait-down",
    10: "dialog-down",
    11: "dialog-up",
    12: "done",
}
FRONTEND_INPUT_CALIBRATION_PC_GRACE_SECONDS = 8.0

LIVE_FRAMEBUFFER_ADDR = 0xA1F82000
LIVE_FRAMEBUFFER_OFFSET_BYTES = 0
LIVE_FRAMEBUFFER_WIDTH = 240
LIVE_FRAMEBUFFER_HEIGHT = 320
LIVE_FRAMEBUFFER_STRIDE_PIXELS = 240
LIVE_FRAMEBUFFER_FORMAT = "rgb565"
LIVE_FRAMEBUFFER_RAW_BYTES = LIVE_FRAMEBUFFER_STRIDE_PIXELS * LIVE_FRAMEBUFFER_HEIGHT * 2

WS_RAW_FRAME_MAGIC = b"BBKRAW1\0"
WS_RAW_FRAME_FORMAT_RGB565 = 1
WS_RAW_FRAME_HEADER = struct.Struct("<8sIHHHH")
WS_RAW_FRAME_HEADER_SIZE = WS_RAW_FRAME_HEADER.size
WS_AUDIO_MAGIC = b"BBKAUD1\0"
WS_AUDIO_FORMAT_S16LE = 1
WS_AUDIO_HEADER = struct.Struct("<8sIIHHI")

FRONTEND_POWER_KEY_CODE = 11
KNOWN_FRONTEND_KEY_CODES = {4, 5, 6, 7, 9, 10, FRONTEND_POWER_KEY_CODE}
FRONTEND_KEY_LEASE_SECONDS = 1.0
SAFE_SHUTDOWN_POWER_HOLD_SECONDS = 3.0
SAFE_SHUTDOWN_TIMEOUT_SECONDS = 20.0
SAFE_SHUTDOWN_POLL_SECONDS = 0.05
FRONTEND_ORIENTATIONS = frozenset({"raw", "rot180", "cw90", "ccw90", "hflip", "vflip"})
WEB_AUDIODEV_ID = "bbk9588-web-none"
DEFAULT_WEB_QEMU_ICOUNT = "shift=auto,align=off,sleep=on"


def web_qemu_timing_options(
    accel: str,
    extra_args: tuple[str, ...],
    *,
    icount: str | None,
) -> tuple[str, tuple[str, ...]]:
    """Keep guest timers proportional to TCG progress under heavy workloads."""

    value = str(icount or "").strip()
    if value.lower() in {"", "0", "false", "none", "off"}:
        return accel, extra_args

    accel_parts = [part.strip() for part in str(accel).split(",") if part.strip()]
    if accel_parts and accel_parts[0].lower() == "tcg":
        accel_parts = [
            "thread=single" if part.lower() == "thread=multi" else part
            for part in accel_parts
        ]
        accel = ",".join(accel_parts)

    has_icount = any(
        str(arg).strip().lower().startswith("-icount") for arg in extra_args
    )
    if has_icount:
        return accel, extra_args
    return accel, (*extra_args, "-icount", value)


def web_qemu_audio_options(
    machine_options: tuple[str, ...],
    extra_args: tuple[str, ...],
    *,
    host_audio: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Mute QEMU's host backend when the browser owns audio playback."""

    has_machine_audiodev = any(
        str(option).partition("=")[0].strip().lower() == "audiodev"
        for option in machine_options
    )
    has_audiodev_arg = any(str(arg).strip().lower() == "-audiodev" for arg in extra_args)
    if host_audio or has_machine_audiodev or has_audiodev_arg:
        return machine_options, extra_args
    return (
        (*machine_options, f"audiodev={WEB_AUDIODEV_ID}"),
        (*extra_args, "-audiodev", f"driver=none,id={WEB_AUDIODEV_ID}"),
    )


def deque_tail(items, limit: int) -> list[object]:
    limit = max(0, int(limit))
    if limit == 0:
        return []
    for _ in range(3):
        try:
            length = len(items)
            start = max(0, length - limit)
            return list(islice(items, start, length))
        except RuntimeError:
            time.sleep(0)
    return []


def display_to_raw_point(
    display_x: int,
    display_y: int,
    display_width: int,
    display_height: int,
    orientation: str,
    raw_width: int = 240,
    raw_height: int = 320,
) -> tuple[int, int]:
    """Map a rendered canvas pixel back to the 9588 raw touchscreen space."""

    display_width = max(1, int(display_width))
    display_height = max(1, int(display_height))
    if orientation in {"cw90", "ccw90"}:
        oriented_width, oriented_height = raw_height, raw_width
    else:
        oriented_width, oriented_height = raw_width, raw_height

    x = max(0, min(display_width - 1, int(display_x)))
    y = max(0, min(display_height - 1, int(display_y)))
    x = max(0, min(oriented_width - 1, x * oriented_width // display_width))
    y = max(0, min(oriented_height - 1, y * oriented_height // display_height))

    if orientation == "rot180":
        raw_x = raw_width - 1 - x
        raw_y = raw_height - 1 - y
    elif orientation == "hflip":
        raw_x = raw_width - 1 - x
        raw_y = y
    elif orientation == "vflip":
        raw_x = x
        raw_y = raw_height - 1 - y
    elif orientation == "cw90":
        raw_x = y
        raw_y = raw_height - 1 - x
    elif orientation == "ccw90":
        raw_x = raw_width - 1 - y
        raw_y = x
    else:
        raw_x = x
        raw_y = y
    return max(0, min(raw_width - 1, raw_x)), max(0, min(raw_height - 1, raw_y))


def raw_to_display_point(
    raw_x: int,
    raw_y: int,
    orientation: str,
    raw_width: int = 240,
    raw_height: int = 320,
) -> tuple[int, int]:
    x = max(0, min(raw_width - 1, int(raw_x)))
    y = max(0, min(raw_height - 1, int(raw_y)))
    if orientation == "rot180":
        return raw_width - 1 - x, raw_height - 1 - y
    if orientation == "hflip":
        return raw_width - 1 - x, y
    if orientation == "vflip":
        return x, raw_height - 1 - y
    if orientation == "cw90":
        return raw_height - 1 - y, x
    if orientation == "ccw90":
        return y, raw_width - 1 - x
    return x, y


def display_to_touch_point(
    display_x: int,
    display_y: int,
    display_width: int,
    display_height: int,
    orientation: str,
    touch_width: int = 240,
    touch_height: int = 320,
) -> tuple[int, int]:
    """Map visible canvas coordinates to C200's touchscreen coordinate space."""

    raw_x, raw_y = display_to_raw_point(
        display_x,
        display_y,
        display_width,
        display_height,
        orientation,
        raw_width=touch_width,
        raw_height=touch_height,
    )
    return raw_to_display_point(
        raw_x,
        raw_y,
        "rot180",
        raw_width=touch_width,
        raw_height=touch_height,
    )


def display_to_panel_point(
    display_x: int,
    display_y: int,
    display_width: int,
    display_height: int,
    panel_width: int = 240,
    panel_height: int = 320,
) -> tuple[int, int]:
    """Map visible canvas coordinates to physical touch-panel coordinates."""

    display_width = max(1, int(display_width))
    display_height = max(1, int(display_height))
    x = max(0, min(display_width - 1, int(display_x)))
    y = max(0, min(display_height - 1, int(display_y)))
    panel_x = x * panel_width // display_width
    panel_y = y * panel_height // display_height
    return max(0, min(panel_width - 1, panel_x)), max(0, min(panel_height - 1, panel_y))


class FrontendState:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.nand_lifecycle_lock = threading.RLock()
        self.lock = threading.RLock()
        self.status_lock = threading.RLock()
        self.frontend_activity_condition = threading.Condition()
        self.audio_activity_condition = threading.Condition()
        self.input_lock = threading.RLock()
        self.frame_io_lock = threading.Lock()

        self.qemu_backend: QemuProcessBackend | None = None
        self.qemu_worker: threading.Thread | None = None
        self.cancel_run = threading.Event()

        self.last_error: str | None = None
        self.crash_snapshot: dict[str, object] | None = None
        self.last_frame: dict[str, object] | None = None
        self.running = False
        self.reset_at = time.time()
        self.safe_shutdown_state = "idle"
        self.safe_shutdown_started_at: float | None = None
        self.safe_shutdown_finished_at: float | None = None
        self.safe_shutdown_error: str | None = None
        self.lifecycle_notice: str | None = None

        self.job_name: str | None = None
        self.job_total_steps = 0
        self.job_done_steps = 0
        self.job_chunk_steps = 0
        self.job_last_slice_steps = 0
        self.job_last_slice_timed_out = False
        self.job_started_at: float | None = None
        self.job_finished_at: float | None = None

        self.input_worker_pending = False
        self.input_wake_count = 0
        self.last_input_event: dict[str, object] | None = None
        self.pending_touches: deque[tuple[int, int, bool]] = deque(maxlen=32)
        self.pending_keys: deque[tuple[int, bool]] = deque(maxlen=32)
        self.frontend_key_leases: dict[tuple[str, int], float] = {}
        self.frontend_active_key_codes: set[int] = set()
        self.frontend_key_lease_expiration_count = 0

        self.frontend_input_calibration_stage = 0
        self.frontend_input_calibration_last_stage_step = -1
        self.qemu_frontend_input_calibration_last_action_at = 0.0
        self.qemu_frontend_input_calibration_log: list[dict[str, object]] = []

        self.frame_push_min_interval = max(0.0, float(getattr(args, "frame_push_min_interval", 1.0 / 30.0)))
        self.frame_info_min_interval = max(0.0, float(getattr(args, "frame_info_min_interval", 1.0)))
        self.frame_push_last_time = 0.0
        self.frame_push_hook_count = 0
        self.frame_push_queued_count = 0
        self.frame_push_throttle_count = 0
        self.frame_push_deferred_count = 0
        self.frame_push_replace_count = 0
        self.frame_push_drop_count = 0
        self.frame_push_error_count = 0
        self.frame_push_last_source_lag_ms: float | None = None
        self.frame_push_max_source_lag_ms = 0.0
        self.frame_info_last_time = 0.0
        self.frame_info_update_count = 0

        self.cached_status: dict[str, object] = {}
        self.cached_frame_bytes: bytes | None = None
        self.cached_frame_time = 0.0
        self.cached_frame_seq: int | None = None
        self.cached_ws_frame_bytes: bytes | None = None
        self.cached_ws_frame_time = 0.0
        self.qemu_last_ws_frame_seq: int | None = None
        self.qemu_legacy_ws_pop_seq: int | None = None

        self.ws_frame_sent_count = 0
        self.ws_frame_sent_bytes = 0
        self.ws_frame_last_seq: int | None = None
        self.ws_frame_last_kind = ""
        self.ws_frame_last_bytes = 0
        self.ws_frame_last_sent_at = 0.0
        self.screen_png_count = 0
        self.screen_png_last_sent_at = 0.0
        self.frontend_performance_metrics: dict[str, object] = {}
        self._perf_last_time: float | None = None
        self._perf_last_ws_frame_sent_count = 0
        self._perf_last_frame_push_queued_count = 0
        self._perf_last_screen_png_count = 0
        self.ws_command_count = 0
        self.ws_last_command_op = ""
        self.ws_last_command_seq: object | None = None
        self.ws_last_command_at = 0.0
        self.ws_reader_alive = False
        self.ws_reader_heartbeat = 0.0
        self.ws_connection_count = 0
        self.ws_connection_peak = 0
        self.frontend_activity_seq = 0
        self.audio_delivery_seq = 0
        self.audio_packets: deque[tuple[int, bytes]] = deque(maxlen=128)
        self.audio_received_packets = 0
        self.audio_received_bytes = 0
        self.audio_dropped_packets = 0
        self.audio_ws_sent_packets = 0
        self.audio_ws_sent_bytes = 0
        self.audio_ws_connection_count = 0
        self.audio_ws_connection_peak = 0
        self.nand_files_lock = self.nand_lifecycle_lock
        self.nand_image_lease = NandImageLease()
        self.nand_files_cache_path = BUILD / "nand_fs_cache" / "bbk9588_fat.img"
        self.nand_files_cache_signature: tuple[str, int, int] | None = None
        self.nand_legacy_checkpoint_migrated: str | None = None
        self.nand_import_result: dict[str, object] | None = None

        try:
            import_source = getattr(args, "nand_import_source", None)
            if import_source is not None:
                if args.nand_image is None:
                    raise ValueError("--nand-import-source requires --nand-image")
                destination = Path(args.nand_image).resolve()
                self.nand_image_lease.acquire(destination)
                self.nand_import_result = import_nand_source(
                    Path(import_source),
                    destination,
                )
            self.reset()
        except Exception:
            self.nand_image_lease.release()
            raise

    def _default_nand_image(self) -> Path | None:
        if bool(getattr(self.args, "no_nand", False)):
            return None
        if self.args.nand_image is not None:
            return self.args.nand_image
        for candidate in COMBINED_NAND_IMAGE_CANDIDATES:
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path.resolve()).lower()

    def _resolve_nand_image_path(self, value: object) -> Path:
        text = os.path.expandvars(str(value or "").strip()).strip('"')
        if not text:
            raise ValueError("missing NAND image path")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"NAND image does not exist: {path}")
        return path

    def nand_image_catalog(self) -> dict[str, object]:
        current = self._default_nand_image()
        current_path = str(current.resolve()) if current is not None else ""
        paths: list[Path] = []
        if getattr(self.args, "nand_image", None) is not None:
            paths.append(Path(self.args.nand_image))
        paths.extend(COMBINED_NAND_IMAGE_CANDIDATES)
        if BUILD.exists():
            for pattern in NAND_IMAGE_GLOB_PATTERNS:
                paths.extend(sorted(BUILD.glob(pattern)))

        seen: set[str] = set()
        images: list[dict[str, object]] = []
        for path in paths:
            resolved = path.resolve()
            key = self._path_key(resolved)
            if key in seen:
                continue
            seen.add(key)
            exists = resolved.is_file()
            item: dict[str, object] = {
                "name": resolved.name,
                "path": str(resolved),
                "exists": exists,
                "current": bool(current_path and key == self._path_key(Path(current_path))),
            }
            if exists:
                try:
                    item["size"] = resolved.stat().st_size
                except OSError:
                    pass
            images.append(item)
        return {"current_path": current_path, "images": images}

    def set_nand_image(self, path: object, *, reset: bool = True) -> dict[str, object]:
        selected = self._resolve_nand_image_path(path)
        with self.nand_lifecycle_lock:
            current = self.nand_image_lease.path
            changed = current is None or self._path_key(current) != self._path_key(selected)
            if changed:
                replacement = NandImageLease()
                replacement.acquire(selected)
                try:
                    validate_nand_image(selected)
                    self.stop()
                except Exception:
                    replacement.release()
                    raise
                previous = self.nand_image_lease
                self.nand_image_lease = replacement
                previous.release()
            self.args.nand_image = selected
            if reset:
                return self.reset()
            with self.lock:
                self._publish_snapshot_locked()
                return self.snapshot()

    def _nand_files_image(self) -> Path:
        selected = self._default_nand_image()
        if selected is None:
            raise FileNotFoundError("active NAND image is missing")
        return selected.resolve()

    def _nand_files_fat_snapshot(self) -> Path:
        image = self._nand_files_image()
        stat = image.stat()
        signature = (str(image), stat.st_size, stat.st_mtime_ns)
        cache_path = getattr(
            self,
            "nand_files_cache_path",
            BUILD / "nand_fs_cache" / "bbk9588_fat.img",
        )
        cached_signature = getattr(self, "nand_files_cache_signature", None)
        if cached_signature != signature or not cache_path.is_file():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            try:
                extract_logical_fat_image(image, temporary)
                os.replace(temporary, cache_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.nand_files_cache_signature = signature
        return cache_path

    def _invalidate_nand_files_cache(self) -> None:
        self.nand_files_cache_signature = None
        cache_path = getattr(self, "nand_files_cache_path", None)
        if isinstance(cache_path, Path):
            cache_path.unlink(missing_ok=True)

    def nand_files_list(self, directory: object = "/") -> dict[str, object]:
        with self.nand_files_lock:
            result = list_fat_directory(self._nand_files_fat_snapshot(), directory)
        result["image"] = str(self._default_nand_image() or "")
        return result

    def nand_file_export(self, file_path: object) -> tuple[str, bytes]:
        with self.nand_files_lock:
            return read_fat_file(self._nand_files_fat_snapshot(), file_path)

    def _mutate_nand_files(self, operation, *, validator=None) -> dict[str, object]:
        with self.nand_files_lock:
            backend = self.qemu_backend
            if backend is not None:
                self.stop()
            image = self._nand_files_image()
            self.nand_image_lease.acquire(image)
            try:
                mutate_nand_files(image, operation, validator=validator)
                self._invalidate_nand_files_cache()
            except Exception:
                if backend is not None:
                    self.reset()
                raise
            status = self.reset() if backend is not None else self.snapshot()
        status["nand_files_changed"] = True
        return status

    def nand_files_mkdir(self, directory: object, name: object) -> dict[str, object]:
        target = join_nand_path(directory, name)

        def operation(fs) -> None:
            fs.makedir(target, recreate=False)

        def validator(fs) -> None:
            if not fs.isdir(target):
                raise ValueError(f"created NAND directory is missing: {target}")

        result = self._mutate_nand_files(operation, validator=validator)
        result["nand_file_path"] = target
        return result

    def nand_files_import(
        self,
        directory: object,
        name: object,
        source_path: Path,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, object]:
        source = source_path.resolve()
        if expected_size < 0 or expected_size > 128 * 1024 * 1024:
            raise ValueError("NAND file upload exceeds 128 MiB")
        if source.stat().st_size != expected_size:
            raise ValueError("temporary NAND upload size changed before import")
        source_digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                source_digest.update(chunk)
        if source_digest.hexdigest() != expected_sha256:
            raise ValueError("temporary NAND upload checksum changed before import")
        target = join_nand_path(directory, name)

        def operation(fs) -> None:
            with source.open("rb") as input_stream, fs.openbin(target, "w") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)

        def validator(fs) -> None:
            if not fs.isfile(target):
                raise ValueError(f"imported NAND file is missing: {target}")
            if fs.getsize(target) != expected_size:
                raise ValueError(f"imported NAND file has the wrong size: {target}")
            digest = hashlib.sha256()
            with fs.openbin(target, "r") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError(f"imported NAND file checksum mismatch: {target}")

        result = self._mutate_nand_files(operation, validator=validator)
        result.update({"nand_file_path": target, "nand_file_size": expected_size})
        return result

    def nand_files_rename(self, file_path: object, name: object) -> dict[str, object]:
        source = normalize_nand_path(file_path, allow_root=False)
        target = join_nand_path(str(PurePosixPath(source).parent), name)

        def operation(fs) -> None:
            if fs.isdir(source):
                fs.movedir(source, target, create=True)
            elif fs.isfile(source):
                fs.move(source, target, overwrite=False)
            else:
                raise FileNotFoundError(f"NAND path does not exist: {source}")

        def validator(fs) -> None:
            if fs.exists(source):
                raise ValueError(f"renamed NAND source still exists: {source}")
            if not fs.exists(target):
                raise ValueError(f"renamed NAND target is missing: {target}")

        result = self._mutate_nand_files(operation, validator=validator)
        result.update({"nand_file_path": target, "nand_file_previous_path": source})
        return result

    def nand_files_delete(self, file_path: object) -> dict[str, object]:
        target = normalize_nand_path(file_path, allow_root=False)

        def operation(fs) -> None:
            if fs.isdir(target):
                fs.removetree(target)
            elif fs.isfile(target):
                fs.remove(target)
            else:
                raise FileNotFoundError(f"NAND path does not exist: {target}")

        def validator(fs) -> None:
            if fs.exists(target):
                raise ValueError(f"deleted NAND path still exists: {target}")

        result = self._mutate_nand_files(operation, validator=validator)
        result["nand_file_deleted"] = target
        return result

    def _reset_runtime_fields_locked(self) -> None:
        self.last_error = None
        self.crash_snapshot = None
        self.safe_shutdown_state = "idle"
        self.safe_shutdown_started_at = None
        self.safe_shutdown_finished_at = None
        self.safe_shutdown_error = None
        self.lifecycle_notice = None
        self.last_frame = {
            "backend": "qemu",
            "available": False,
            "reason": "no frame captured yet",
            "output_width": LIVE_FRAMEBUFFER_WIDTH,
            "output_height": LIVE_FRAMEBUFFER_HEIGHT,
        }
        self.running = False
        self.job_name = "qemu"
        self.job_total_steps = 0
        self.job_done_steps = 0
        self.job_chunk_steps = 0
        self.job_last_slice_steps = 0
        self.job_last_slice_timed_out = False
        self.job_started_at = time.time()
        self.job_finished_at = None
        self.input_worker_pending = False
        self.input_wake_count = 0
        self.last_input_event = None
        self.frontend_key_leases.clear()
        self.frontend_active_key_codes.clear()
        self.frontend_key_lease_expiration_count = 0
        self.frontend_input_calibration_stage = 0
        self.frontend_input_calibration_last_stage_step = -1
        self.qemu_frontend_input_calibration_last_action_at = 0.0
        self.qemu_frontend_input_calibration_log = []
        self.reset_at = time.time()
        self.cached_frame_bytes = None
        self.cached_frame_time = 0.0
        self.cached_frame_seq = None
        self.cached_ws_frame_bytes = None
        self.cached_ws_frame_time = 0.0
        self.qemu_last_ws_frame_seq = None
        self.qemu_legacy_ws_pop_seq = None
        self.frame_push_last_time = 0.0
        self.frame_push_hook_count = 0
        self.frame_push_queued_count = 0
        self.frame_push_throttle_count = 0
        self.frame_push_deferred_count = 0
        self.frame_push_replace_count = 0
        self.frame_push_drop_count = 0
        self.frame_push_error_count = 0
        self.frame_push_last_source_lag_ms = None
        self.frame_push_max_source_lag_ms = 0.0
        self.frame_info_last_time = 0.0
        self.frame_info_update_count = 0
        self.ws_frame_sent_count = 0
        self.ws_frame_sent_bytes = 0
        self.ws_frame_last_seq = None
        self.ws_frame_last_kind = ""
        self.ws_frame_last_bytes = 0
        self.ws_frame_last_sent_at = 0.0
        self.screen_png_count = 0
        self.screen_png_last_sent_at = 0.0
        self.frontend_performance_metrics = {}
        self._perf_last_time = None
        self._perf_last_ws_frame_sent_count = 0
        self._perf_last_frame_push_queued_count = 0
        self._perf_last_screen_png_count = 0
        with self.audio_activity_condition:
            self.audio_packets.clear()
            self.audio_received_packets = 0
            self.audio_received_bytes = 0
            self.audio_dropped_packets = 0
            self.audio_ws_sent_packets = 0
            self.audio_ws_sent_bytes = 0
            self.audio_activity_condition.notify_all()
        with self.input_lock:
            self.pending_touches.clear()
            self.pending_keys.clear()

    def reset(self) -> dict[str, object]:
        with self.nand_lifecycle_lock:
            return self._reset_nand_locked()

    def _set_safe_shutdown_state(
        self,
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        with self.lock:
            now = time.time()
            if state == "requesting":
                self.safe_shutdown_started_at = now
                self.safe_shutdown_finished_at = None
                self.lifecycle_notice = None
            elif state in {"complete", "failed", "forced"}:
                self.safe_shutdown_finished_at = now
            self.safe_shutdown_state = state
            self.safe_shutdown_error = error
            self._publish_snapshot_locked()

    def _request_guest_shutdown_locked(
        self,
        *,
        hold_seconds: float = SAFE_SHUTDOWN_POWER_HOLD_SECONDS,
        timeout: float = SAFE_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        backend = self.qemu_backend
        if backend is None:
            return {}
        initial = self._backend_snapshot(backend, refresh=False)
        if not initial.get("running"):
            return initial

        hold_seconds = max(0.0, float(hold_seconds))
        timeout = max(hold_seconds, float(timeout))
        deadline = time.monotonic() + timeout
        self._set_safe_shutdown_state("requesting")

        with self.lock:
            for token in [
                token
                for token in self.frontend_key_leases
                if token[1] == FRONTEND_POWER_KEY_CODE
            ]:
                self.frontend_key_leases.pop(token, None)
            self.frontend_active_key_codes.discard(FRONTEND_POWER_KEY_CODE)
            result, _event = self._record_frontend_key_transition_locked(
                backend,
                FRONTEND_POWER_KEY_CODE,
                True,
                source="safe-shutdown",
            )
        if not result.get("applied"):
            error = str(result.get("error") or "QEMU did not accept the power key")
            self._set_safe_shutdown_state("failed", error=error)
            raise RuntimeError(f"safe shutdown failed: {error}")

        released = False
        try:
            hold_deadline = min(deadline, time.monotonic() + hold_seconds)
            while time.monotonic() < hold_deadline:
                snapshot = self._backend_snapshot(backend, refresh=False)
                if not snapshot.get("running"):
                    break
                time.sleep(SAFE_SHUTDOWN_POLL_SECONDS)
            snapshot = self._backend_snapshot(backend, refresh=False)
            if snapshot.get("running"):
                with self.lock:
                    self._record_frontend_key_transition_locked(
                        backend,
                        FRONTEND_POWER_KEY_CODE,
                        False,
                        source="safe-shutdown",
                    )
                released = True
                self._set_safe_shutdown_state("waiting")

            while time.monotonic() < deadline:
                snapshot = self._backend_snapshot(backend, refresh=False)
                if not snapshot.get("running"):
                    if snapshot.get("exit_reason") == "guest-shutdown":
                        break
                    # Give the QMP reader a short window to consume SHUTDOWN.
                    time.sleep(SAFE_SHUTDOWN_POLL_SECONDS)
                    continue
                time.sleep(SAFE_SHUTDOWN_POLL_SECONDS)

            snapshot = self._backend_snapshot(backend, refresh=False)
            if snapshot.get("running"):
                raise TimeoutError(
                    f"firmware did not complete guest shutdown within {timeout:.1f}s"
                )
            if snapshot.get("exit_reason") != "guest-shutdown":
                raise RuntimeError(
                    "QEMU exited without the firmware guest-shutdown signal "
                    f"(reason={snapshot.get('exit_reason')!r}, "
                    f"returncode={snapshot.get('returncode')!r})"
                )
        except Exception as exc:
            if not released and self._backend_snapshot(backend, refresh=False).get("running"):
                try:
                    with self.lock:
                        self._record_frontend_key_transition_locked(
                            backend,
                            FRONTEND_POWER_KEY_CODE,
                            False,
                            source="safe-shutdown-error",
                        )
                except Exception:
                    pass
            error = f"{type(exc).__name__}: {exc}"
            self._set_safe_shutdown_state("failed", error=error)
            raise RuntimeError(f"safe shutdown failed: {error}") from exc

        self._set_safe_shutdown_state("complete")
        return snapshot

    def _stop_backend_for_reset_locked(
        self,
        backend: QemuProcessBackend,
        *,
        shutdown_timeout: float = SAFE_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> tuple[str, str] | None:
        snapshot = self._backend_snapshot(backend, refresh=False)
        if not snapshot.get("running"):
            return None

        frame_count = snapshot.get("frame_chardev_count")
        if isinstance(frame_count, (int, float)) and frame_count <= 0:
            backend.stop()
            return (
                "固件尚未完成启动，已由 QEMU 正常停止并刷新 NAND 后重新启动。",
                "firmware startup had not produced a frame; guest shutdown was unavailable",
            )

        try:
            self._request_guest_shutdown_locked(timeout=shutdown_timeout)
        except RuntimeError as exc:
            backend.stop()
            return (
                "固件未完成关机，已由 QEMU 正常停止并刷新 NAND 后重新启动。",
                str(exc),
            )
        return None

    def _reset_nand_locked(self) -> dict[str, object]:
        old_backend = self.qemu_backend
        restart_fallback: tuple[str, str] | None = None
        if old_backend is not None and self._backend_snapshot(old_backend, refresh=False).get("running"):
            restart_fallback = self._stop_backend_for_reset_locked(old_backend)
        if old_backend is not None:
            callback_setter = getattr(old_backend, "set_frame_ready_callback", None)
            if callback_setter is not None:
                callback_setter(None)
            audio_callback_setter = getattr(old_backend, "set_audio_ready_callback", None)
            if audio_callback_setter is not None:
                audio_callback_setter(None)
        self.cancel_run.set()
        if self.qemu_worker is not None and self.qemu_worker.is_alive() and self.qemu_worker is not threading.current_thread():
            self.qemu_worker.join(timeout=2.0)

        nand_image = self._default_nand_image()
        if nand_image is not None:
            self.nand_image_lease.acquire(nand_image)
            migrated = migrate_legacy_nand_checkpoint(nand_image)
            if migrated is not None:
                self.nand_legacy_checkpoint_migrated = str(migrated)
        else:
            self.nand_image_lease.release()

        with self.lock:
            BUILD.mkdir(parents=True, exist_ok=True)
            self.cancel_run.clear()
            self._reset_runtime_fields_locked()
            if restart_fallback is not None:
                self.safe_shutdown_state = "host-fallback"
                self.safe_shutdown_finished_at = time.time()
                self.safe_shutdown_error = restart_fallback[1]
                self.lifecycle_notice = restart_fallback[0]
            extra_args = tuple(getattr(self.args, "qemu_extra_arg", []) or ())
            accel, extra_args = web_qemu_timing_options(
                getattr(self.args, "qemu_accel", "tcg,thread=multi,tb-size=256"),
                extra_args,
                icount=getattr(self.args, "qemu_icount", DEFAULT_WEB_QEMU_ICOUNT),
            )
            machine_options, extra_args = web_qemu_audio_options(
                tuple(getattr(self.args, "qemu_machine_option", []) or ()),
                extra_args,
                host_audio=bool(getattr(self.args, "qemu_host_audio", False)),
            )
            config = build_bbk_qemu_config(
                boot_mode=getattr(self.args, "boot_mode", "nand"),
                executable=getattr(self.args, "qemu", DEFAULT_QEMU_EXECUTABLE),
                image=getattr(self.args, "image", None),
                payload=getattr(self.args, "payload", None),
                ram_mb=int(getattr(self.args, "ram_mb", 160)),
                machine=getattr(self.args, "qemu_machine", DEFAULT_QEMU_MACHINE),
                cpu=getattr(self.args, "qemu_cpu", "24Kf"),
                accel=accel,
                display="none",
                serial="mon:stdio",
                monitor="none",
                gdb=getattr(self.args, "qemu_gdb", "none"),
                timeout_seconds=float(getattr(self.args, "qemu_timeout", 5.0)),
                nand_image=nand_image,
                hibernate_wakeup=getattr(self.args, "boot_mode", "nand") == "nand",
                bbk_machine_options=machine_options,
                extra_args=extra_args,
                firmware_patches=getattr(self.args, "qemu_firmware_patch", None),
            )
            self.qemu_backend = QemuProcessBackend(config)
            self.qemu_backend.set_frame_ready_callback(self._on_qemu_frame_ready)
            self.qemu_backend.set_audio_ready_callback(self._on_qemu_audio_ready)
            try:
                self.qemu_backend.start()
                self.running = bool(self._backend_snapshot(self.qemu_backend, refresh=False).get("running"))
                self._start_qemu_tick_worker_locked()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.job_finished_at = time.time()
                self.running = False
            self._publish_snapshot_locked()
            return self.snapshot()

    def _start_qemu_tick_worker_locked(self) -> None:
        if self.qemu_worker is not None and self.qemu_worker.is_alive():
            return

        def worker() -> None:
            while not self.cancel_run.is_set():
                try:
                    with self.lock:
                        backend = self.qemu_backend
                        if backend is None:
                            break
                        qemu = self._backend_snapshot(backend, refresh=False)
                        if not qemu.get("running"):
                            self.running = False
                            if self.job_finished_at is None:
                                self.job_finished_at = time.time()
                            self._publish_snapshot_locked()
                            break
                        self._apply_frontend_input_calibration_locked(backend)
                except Exception as exc:
                    with self.lock:
                        self.qemu_frontend_input_calibration_log.append(
                            {"event": "qemu-frontend-input-worker-error", "error": f"{type(exc).__name__}: {exc}"}
                        )
                        del self.qemu_frontend_input_calibration_log[:-16]
                self._notify_frontend_activity()
                time.sleep(0.5)

        self.qemu_worker = threading.Thread(target=worker, name="bbk9588-qemu-frontend-tick", daemon=True)
        self.qemu_worker.start()

    def _ensure_qemu_started_locked(self) -> QemuProcessBackend:
        if self.qemu_backend is None:
            self.reset()
        assert self.qemu_backend is not None
        qemu = self._backend_snapshot(self.qemu_backend, refresh=False)
        if not qemu.get("running"):
            qemu = self._backend_snapshot(self.qemu_backend, refresh=True)
        if not qemu.get("running") and qemu.get("returncode") is not None:
            self.qemu_backend.start()
            self._start_qemu_tick_worker_locked()
            qemu = self._backend_snapshot(self.qemu_backend, refresh=False)
        self.running = bool(qemu.get("running"))
        return self.qemu_backend

    def _ws_payload_from_raw_frame(self, seq: int, raw: bytes) -> bytes:
        if len(raw) < LIVE_FRAMEBUFFER_RAW_BYTES:
            raise ValueError(f"short RGB565 frame: {len(raw)} bytes")
        payload = raw[:LIVE_FRAMEBUFFER_RAW_BYTES]
        return WS_RAW_FRAME_HEADER.pack(
            WS_RAW_FRAME_MAGIC,
            int(seq) & 0xFFFFFFFF,
            LIVE_FRAMEBUFFER_WIDTH,
            LIVE_FRAMEBUFFER_HEIGHT,
            LIVE_FRAMEBUFFER_STRIDE_PIXELS,
            WS_RAW_FRAME_FORMAT_RGB565,
        ) + payload

    def _latest_qemu_raw_frame_locked(self) -> tuple[int, float, bytes] | None:
        backend = self.qemu_backend
        if backend is None:
            return None
        latest = backend.latest_frame_chardev
        if latest is not None:
            return latest
        return None

    def _on_qemu_frame_ready(self) -> None:
        self.frame_push_hook_count += 1
        self._notify_frontend_activity()

    def _on_qemu_audio_ready(
        self,
        qemu_seq: int,
        sample_rate: int,
        channels: int,
        pcm: bytes,
    ) -> None:
        packet = WS_AUDIO_HEADER.pack(
            WS_AUDIO_MAGIC,
            int(qemu_seq) & 0xFFFFFFFF,
            int(sample_rate),
            int(channels),
            16,
            len(pcm),
        ) + pcm
        with self.audio_activity_condition:
            self.audio_delivery_seq += 1
            self.audio_packets.append((self.audio_delivery_seq, packet))
            self.audio_received_packets += 1
            self.audio_received_bytes += len(pcm)
            self.audio_activity_condition.notify_all()

    def latest_audio_sequence(self) -> int:
        with self.audio_activity_condition:
            return self.audio_delivery_seq

    def audio_packets_after(
        self,
        last_seq: int,
        max_packets: int = 12,
    ) -> list[tuple[int, bytes]]:
        with self.audio_activity_condition:
            packets = [entry for entry in self.audio_packets if entry[0] > last_seq]
            max_packets = max(1, int(max_packets))
            if len(packets) > max_packets:
                dropped = len(packets) - max_packets
                self.audio_dropped_packets += dropped
                packets = packets[-max_packets:]
            return packets

    def wait_for_audio_activity(self, last_seq: int, timeout: float) -> int:
        deadline = time.time() + max(0.0, timeout)
        with self.audio_activity_condition:
            while self.audio_delivery_seq == last_seq:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.audio_activity_condition.wait(timeout=remaining)
            return self.audio_delivery_seq

    def register_audio_ws_connection(self) -> None:
        with self.audio_activity_condition:
            self.audio_ws_connection_count += 1
            self.audio_ws_connection_peak = max(
                self.audio_ws_connection_peak,
                self.audio_ws_connection_count,
            )

    def unregister_audio_ws_connection(self) -> None:
        with self.audio_activity_condition:
            self.audio_ws_connection_count = max(0, self.audio_ws_connection_count - 1)

    def record_audio_ws_sent(self, payload_bytes: int) -> None:
        with self.audio_activity_condition:
            self.audio_ws_sent_packets += 1
            self.audio_ws_sent_bytes += max(0, int(payload_bytes))

    def _audio_transport_snapshot(self) -> dict[str, object]:
        with self.audio_activity_condition:
            return {
                "received_packets": self.audio_received_packets,
                "received_bytes": self.audio_received_bytes,
                "queued_packets": len(self.audio_packets),
                "dropped_packets": self.audio_dropped_packets,
                "ws_sent_packets": self.audio_ws_sent_packets,
                "ws_sent_bytes": self.audio_ws_sent_bytes,
                "ws_connections": self.audio_ws_connection_count,
                "ws_connection_peak": self.audio_ws_connection_peak,
            }

    def pop_queued_frame(self) -> bytes | None:
        return None

    def pop_latest_queued_frame(self) -> bytes | None:
        return self.pop_queued_frame()

    def latest_ws_frame_after(self, last_seq: int | None) -> tuple[int, bytes] | None:
        with self.lock:
            latest = self._latest_qemu_raw_frame_locked()
            if latest is None:
                return None
            seq, captured_at, raw = latest
            if last_seq == seq:
                return None
            if self.qemu_last_ws_frame_seq == seq and self.cached_ws_frame_bytes is not None:
                return seq, self.cached_ws_frame_bytes
            now = time.time()
            if self.qemu_last_ws_frame_seq is not None and now - self.frame_push_last_time < self.frame_push_min_interval:
                self.frame_push_throttle_count += 1
                return None
            try:
                payload = self._ws_payload_from_raw_frame(seq, raw)
            except Exception as exc:
                self.frame_push_error_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                return None
            previous_seq = self.qemu_last_ws_frame_seq
            self.qemu_last_ws_frame_seq = seq
            self.cached_ws_frame_bytes = payload
            self.cached_ws_frame_time = captured_at
            if previous_seq is not None:
                source_gap = (int(seq) - int(previous_seq)) & 0xFFFFFFFF
                if 1 < source_gap < 0x80000000:
                    self.frame_push_replace_count += source_gap - 1
            source_lag_ms = max(0.0, (now - captured_at) * 1000.0)
            self.frame_push_last_source_lag_ms = source_lag_ms
            self.frame_push_max_source_lag_ms = max(self.frame_push_max_source_lag_ms, source_lag_ms)
            self.frame_push_queued_count += 1
            self.frame_push_last_time = now
            self._notify_frontend_activity()
            return seq, payload

    def pop_queued_ws_frame(self) -> bytes | None:
        latest = self.latest_ws_frame_after(self.qemu_legacy_ws_pop_seq)
        if latest is None:
            return None
        seq, payload = latest
        self.qemu_legacy_ws_pop_seq = seq
        return payload

    def pop_latest_queued_ws_frame(self) -> bytes | None:
        return self.pop_queued_ws_frame()

    def _cache_ws_raw_frame_locked(self, seq: int, captured_at: float, raw: bytes, source: str) -> bytes:
        payload = self._ws_payload_from_raw_frame(seq, raw)
        self.cached_ws_frame_bytes = payload
        self.cached_ws_frame_time = captured_at
        self.qemu_last_ws_frame_seq = seq
        self.last_frame = self._frame_info_from_raw(raw, source)
        return payload

    def _cache_png_raw_frame_locked(
        self,
        raw: bytes,
        source: str,
        captured_at: float | None = None,
        seq: int | None = None,
    ) -> bytes:
        self.last_frame, rgb = rgb565_raw_to_info_rgb(
            raw,
            LIVE_FRAMEBUFFER_ADDR,
            LIVE_FRAMEBUFFER_OFFSET_BYTES,
            LIVE_FRAMEBUFFER_WIDTH,
            LIVE_FRAMEBUFFER_HEIGHT,
            LIVE_FRAMEBUFFER_STRIDE_PIXELS,
            LIVE_FRAMEBUFFER_FORMAT,
            getattr(self.args, "orientation", "rot180"),
        )
        self.last_frame["backend"] = "qemu"
        self.last_frame["source"] = source
        self.last_frame["available"] = True
        frame = png_bytes_from_rgb(int(self.last_frame["output_width"]), int(self.last_frame["output_height"]), rgb)
        self.cached_frame_bytes = frame
        self.cached_frame_time = time.time() if captured_at is None else captured_at
        self.cached_frame_seq = seq
        return frame

    def _placeholder_png_frame_locked(self, reason: str) -> bytes:
        self.last_frame = {
            "backend": "qemu",
            "available": False,
            "reason": reason,
            "output_width": LIVE_FRAMEBUFFER_WIDTH,
            "output_height": LIVE_FRAMEBUFFER_HEIGHT,
        }
        rgb = bytes([0x12, 0x16, 0x1C]) * (LIVE_FRAMEBUFFER_WIDTH * LIVE_FRAMEBUFFER_HEIGHT)
        frame = png_bytes_from_rgb(LIVE_FRAMEBUFFER_WIDTH, LIVE_FRAMEBUFFER_HEIGHT, rgb)
        self.cached_frame_bytes = frame
        self.cached_frame_time = time.time()
        self.cached_frame_seq = None
        return frame

    def _placeholder_ws_frame_locked(self, reason: str) -> bytes:
        self.last_frame = {
            "backend": "qemu",
            "available": False,
            "reason": reason,
            "output_width": LIVE_FRAMEBUFFER_WIDTH,
            "output_height": LIVE_FRAMEBUFFER_HEIGHT,
        }
        raw = b"\x00\x00" * (LIVE_FRAMEBUFFER_WIDTH * LIVE_FRAMEBUFFER_HEIGHT)
        seq = int(time.time() * 1000)
        payload = self._ws_payload_from_raw_frame(seq, raw)
        self.cached_ws_frame_bytes = payload
        self.cached_ws_frame_time = time.time()
        self.qemu_last_ws_frame_seq = seq
        return payload

    def dump_ws_frame(self) -> bytes:
        backend: QemuProcessBackend
        with self.lock:
            backend = self._ensure_qemu_started_locked()
            latest = self._latest_qemu_raw_frame_locked()
            if latest is not None:
                seq, captured_at, raw = latest
                payload = self._cache_ws_raw_frame_locked(seq, captured_at, raw, "qemu-frame-chardev")
                self._notify_frontend_activity()
                return payload
            if self.cached_ws_frame_bytes is not None:
                return self.cached_ws_frame_bytes

        if not self.frame_io_lock.acquire(blocking=False):
            with self.lock:
                if self.cached_ws_frame_bytes is not None:
                    return self.cached_ws_frame_bytes
                payload = self._placeholder_ws_frame_locked("frame read already in progress")
                self._notify_frontend_activity()
                return payload
        try:
            try:
                raw, source = backend.read_display_rgb565_frame()
            except Exception as exc:
                with self.lock:
                    self.last_error = f"frame {type(exc).__name__}: {exc}"
                    payload = self._placeholder_ws_frame_locked(self.last_error)
                    self._notify_frontend_activity()
                    return payload
            with self.lock:
                seq = int(backend.frame_chardev_count or time.time() * 1000)
                payload = self._cache_ws_raw_frame_locked(seq, time.time(), raw, source)
                self._notify_frontend_activity()
                return payload
        finally:
            self.frame_io_lock.release()

    def cached_frame(self) -> bytes | None:
        return self.cached_frame_bytes

    def cached_ws_frame(self) -> bytes | None:
        return self.cached_ws_frame_bytes

    def record_ws_frame_sent(self, payload: bytes) -> None:
        self.ws_frame_sent_count += 1
        self.ws_frame_sent_bytes += len(payload)
        self.ws_frame_last_bytes = len(payload)
        self.ws_frame_last_sent_at = time.time()
        if payload.startswith(WS_RAW_FRAME_MAGIC) and len(payload) >= WS_RAW_FRAME_HEADER_SIZE:
            self.ws_frame_last_kind = "raw-rgb565"
            self.ws_frame_last_seq = int.from_bytes(payload[8:12], "little")
        elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
            self.ws_frame_last_kind = "png"
            self.ws_frame_last_seq = None
        else:
            self.ws_frame_last_kind = "unknown"
            self.ws_frame_last_seq = None

    def _record_screen_png_dump_locked(self, now: float) -> None:
        self.screen_png_count += 1
        self.screen_png_last_sent_at = now

    def _frontend_performance_snapshot_locked(self, now: float, elapsed: float) -> dict[str, object]:
        interval = (
            max(0.0, now - self._perf_last_time)
            if self._perf_last_time is not None
            else None
        )
        websocket_fps: float | None = None
        websocket_transport_fps: float | None = None
        screen_png_fps: float | None = None
        if interval is not None and interval > 0:
            websocket_fps = max(
                0,
                self.frame_push_queued_count - self._perf_last_frame_push_queued_count,
            ) / interval
            websocket_transport_fps = max(
                0,
                self.ws_frame_sent_count - self._perf_last_ws_frame_sent_count,
            ) / interval
            screen_png_fps = max(
                0,
                self.screen_png_count - self._perf_last_screen_png_count,
            ) / interval
        websocket_average_fps = self.frame_push_queued_count / elapsed if elapsed > 0 else None
        websocket_transport_average_fps = self.ws_frame_sent_count / elapsed if elapsed > 0 else None
        screen_png_average_fps = self.screen_png_count / elapsed if elapsed > 0 else None
        metrics: dict[str, object] = {
            "sampled_at": now,
            "sample_interval_seconds": None if interval is None else round(interval, 3),
            "websocket_fps": None if websocket_fps is None else round(websocket_fps, 2),
            "websocket_average_fps": (
                None if websocket_average_fps is None else round(websocket_average_fps, 2)
            ),
            "websocket_transport_fps": (
                None if websocket_transport_fps is None else round(websocket_transport_fps, 2)
            ),
            "websocket_transport_average_fps": (
                None
                if websocket_transport_average_fps is None
                else round(websocket_transport_average_fps, 2)
            ),
            "screen_png_fps": None if screen_png_fps is None else round(screen_png_fps, 2),
            "screen_png_average_fps": (
                None if screen_png_average_fps is None else round(screen_png_average_fps, 2)
            ),
            "screen_png_count": self.screen_png_count,
            "screen_png_last_sent_at": self.screen_png_last_sent_at,
        }
        self.frontend_performance_metrics = metrics
        self._perf_last_time = now
        self._perf_last_ws_frame_sent_count = self.ws_frame_sent_count
        self._perf_last_frame_push_queued_count = self.frame_push_queued_count
        self._perf_last_screen_png_count = self.screen_png_count
        return metrics

    def seconds_until_deferred_frame(self) -> float | None:
        if self.frame_push_min_interval <= 0:
            return None
        remaining = self.frame_push_min_interval - (time.time() - self.frame_push_last_time)
        return remaining if remaining > 0 else None

    def _notify_frontend_activity(self) -> None:
        with self.frontend_activity_condition:
            self.frontend_activity_seq += 1
            self.frontend_activity_condition.notify_all()

    def notify_frontend_activity(self) -> None:
        self._notify_frontend_activity()

    def frontend_activity_sequence(self) -> int:
        with self.frontend_activity_condition:
            return self.frontend_activity_seq

    def wait_for_frontend_activity(self, last_seq: int, timeout: float) -> int:
        deadline = time.time() + max(0.0, timeout)
        with self.frontend_activity_condition:
            while self.frontend_activity_seq == last_seq:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.frontend_activity_condition.wait(timeout=remaining)
            return self.frontend_activity_seq

    def record_ws_command(self, op: str, command_seq: object | None = None) -> None:
        self.ws_command_count += 1
        self.ws_last_command_op = str(op)
        self.ws_last_command_seq = command_seq
        self.ws_last_command_at = time.time()
        self._notify_frontend_activity()

    def set_ws_reader_alive(self, alive: bool) -> None:
        self.ws_reader_alive = bool(alive)
        self.ws_reader_heartbeat = time.time()
        self._notify_frontend_activity()

    def register_ws_connection(self) -> None:
        with self.frontend_activity_condition:
            self.ws_connection_count += 1
            self.ws_connection_peak = max(self.ws_connection_peak, self.ws_connection_count)
            self.ws_reader_alive = True
            self.ws_reader_heartbeat = time.time()
            self.frontend_activity_seq += 1
            self.frontend_activity_condition.notify_all()

    def unregister_ws_connection(self) -> None:
        with self.frontend_activity_condition:
            self.ws_connection_count = max(0, self.ws_connection_count - 1)
            self.ws_reader_alive = self.ws_connection_count > 0
            self.ws_reader_heartbeat = time.time()
            self.frontend_activity_seq += 1
            self.frontend_activity_condition.notify_all()

    def worker_active(self) -> bool:
        backend = self.qemu_backend
        return False if backend is None else bool(self._backend_snapshot(backend, refresh=False).get("running"))

    @staticmethod
    def _backend_snapshot(backend: QemuProcessBackend, *, refresh: bool) -> dict[str, object]:
        try:
            return backend.snapshot(refresh=refresh)
        except TypeError:
            return backend.snapshot()

    def step(self, steps: int) -> dict[str, object]:
        return self.run_start("qemu-step", max(0, int(steps)), max(0, int(steps)))

    def boot(self) -> dict[str, object]:
        return self.run_start("boot", 0, 0)

    def save_checkpoint(self, path: Path) -> dict[str, object]:
        return {"error": "QEMU process backend does not support Python checkpoints", "path": str(path)}

    def run_start(self, name: str, total_steps: int, chunk_steps: int) -> dict[str, object]:
        with self.nand_lifecycle_lock:
            with self.lock:
                try:
                    backend = self._ensure_qemu_started_locked()
                    self.running = bool(self._backend_snapshot(backend, refresh=False).get("running"))
                    self.job_name = name or "qemu"
                    self.job_total_steps = max(0, int(total_steps))
                    self.job_done_steps = 0
                    self.job_chunk_steps = max(0, int(chunk_steps))
                    self.job_last_slice_steps = 0
                    self.job_last_slice_timed_out = False
                    self.job_started_at = self.job_started_at or time.time()
                    self.job_finished_at = None
                    self._start_qemu_tick_worker_locked()
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.running = False
                self._publish_snapshot_locked()
                return self.snapshot()

    def stop(self) -> dict[str, object]:
        with self.nand_lifecycle_lock:
            backend = self.qemu_backend
            if backend is not None and self._backend_snapshot(backend, refresh=False).get("running"):
                self._request_guest_shutdown_locked()
            self.cancel_run.set()
            if (
                self.qemu_worker is not None
                and self.qemu_worker.is_alive()
                and self.qemu_worker is not threading.current_thread()
            ):
                self.qemu_worker.join(timeout=2.0)
            with self.lock:
                self.running = False
                self.job_finished_at = time.time()
                self._publish_snapshot_locked()
                return self.snapshot()

    def force_stop(self) -> dict[str, object]:
        with self.nand_lifecycle_lock:
            self._set_safe_shutdown_state(
                "forced",
                error="QEMU was stopped without waiting for firmware shutdown",
            )
            self.cancel_run.set()
            with self.lock:
                backend = self.qemu_backend
                if backend is not None:
                    backend.stop()
                self.running = False
                self.job_finished_at = time.time()
                self._publish_snapshot_locked()
                return self.snapshot()

    def close(self) -> None:
        with self.nand_lifecycle_lock:
            try:
                try:
                    self.stop()
                except Exception:
                    self.force_stop()
            finally:
                self.nand_image_lease.release()

    @staticmethod
    def _coerce_optional_bool(value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "down"}:
            return True
        if text in {"0", "false", "no", "off", "up"}:
            return False
        return None

    def _touch_coordinates_from_message(self, msg: dict[str, object]) -> tuple[int, int]:
        if "display_x" in msg or "display_y" in msg:
            return display_to_touch_point(
                int(msg.get("display_x", 0)),
                int(msg.get("display_y", 0)),
                int(msg.get("display_width", 240)),
                int(msg.get("display_height", 320)),
                getattr(self.args, "orientation", "rot180"),
            )
        return int(msg.get("x", 0)), int(msg.get("y", 0))

    def _record_frontend_key_transition_locked(
        self,
        backend: QemuProcessBackend,
        code: int,
        down: bool,
        *,
        source: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        result = backend.apply_gui_key_event(code, down)
        event: dict[str, object] = {
            "kind": "key",
            "code": code,
            "down": down,
            "accepted": bool(result.get("applied")),
            "result": result,
            "at": time.time(),
            "source": source,
        }
        self.last_input_event = event
        self.input_wake_count += 1
        with self.input_lock:
            self.pending_keys.clear()
        return result, event

    def _expire_frontend_key_leases_locked(self, now: float | None = None) -> None:
        if not self.frontend_key_leases:
            return
        current = time.monotonic() if now is None else float(now)
        expired = [
            token
            for token, deadline in self.frontend_key_leases.items()
            if deadline <= current
        ]
        if not expired:
            return
        previously_active = set(self.frontend_active_key_codes)
        for token in expired:
            self.frontend_key_leases.pop(token, None)
        self.frontend_active_key_codes = {
            code for _session, code in self.frontend_key_leases
        }
        self.frontend_key_lease_expiration_count += len(expired)
        backend = self.qemu_backend
        if backend is None or not backend.running():
            return
        for code in sorted(previously_active - self.frontend_active_key_codes):
            self._record_frontend_key_transition_locked(
                backend,
                code,
                False,
                source="frontend-key-lease-expired",
            )

    def key(
        self,
        code: int,
        down: bool = True,
        advance: bool | None = None,
        *,
        include_snapshot: bool = True,
        input_session: object = None,
        source: object = None,
    ) -> dict[str, object]:
        code = int(code)
        if code not in KNOWN_FRONTEND_KEY_CODES:
            return {"error": f"unknown key code {code}", "known": sorted(KNOWN_FRONTEND_KEY_CODES)}
        with self.lock:
            backend = self._ensure_qemu_started_locked()
            now = time.monotonic()
            self._expire_frontend_key_leases_locked(now)
            requested_down = bool(down)
            session = str(input_session or "").strip()[:128]
            coalesced = False
            effective_down = requested_down
            if session:
                was_active = code in self.frontend_active_key_codes
                token = (session, code)
                if requested_down:
                    self.frontend_key_leases[token] = now + FRONTEND_KEY_LEASE_SECONDS
                else:
                    self.frontend_key_leases.pop(token, None)
                self.frontend_active_key_codes = {
                    active_code for _active_session, active_code in self.frontend_key_leases
                }
                effective_down = code in self.frontend_active_key_codes
                coalesced = was_active == effective_down
            else:
                for token in [token for token in self.frontend_key_leases if token[1] == code]:
                    self.frontend_key_leases.pop(token, None)
                self.frontend_active_key_codes.discard(code)

            if coalesced:
                result: dict[str, object] = {
                    "event": "qemu-gui-key",
                    "code": code,
                    "down": effective_down,
                    "applied": True,
                    "coalesced": True,
                    "source": "frontend-key-lease",
                }
                accepted = True
            else:
                result, event = self._record_frontend_key_transition_locked(
                    backend,
                    code,
                    effective_down,
                    source=str(source or "message"),
                )
                accepted = bool(event["accepted"])
            if not include_snapshot:
                self._notify_frontend_activity()
                return {
                    "input_accepted": accepted,
                    "qemu_input_result": result,
                }
            self._publish_snapshot_locked()
            snapshot = self.snapshot()
            snapshot["input_accepted"] = accepted
            snapshot["qemu_input_result"] = result
            if advance is not None:
                snapshot["warning"] = "advance is ignored by the QEMU process backend"
            return snapshot

    def touch(
        self,
        x: int,
        y: int,
        down: bool,
        advance: bool | None = None,
        *,
        include_snapshot: bool = True,
    ) -> dict[str, object]:
        x = max(0, min(239, int(x)))
        y = max(0, min(319, int(y)))
        with self.lock:
            backend = self._ensure_qemu_started_locked()
            result = backend.apply_touch_state(x, y, bool(down))
            event = {
                "kind": "touch",
                "x": x,
                "y": y,
                "down": bool(down),
                "accepted": bool(result.get("applied")),
                "result": result,
                "at": time.time(),
            }
            self.last_input_event = event
            self.input_wake_count += 1
            with self.input_lock:
                self.pending_touches.clear()
            if not include_snapshot:
                self._notify_frontend_activity()
                return {
                    "input_accepted": event["accepted"],
                    "qemu_input_result": result,
                }
            self._publish_snapshot_locked()
            snapshot = self.snapshot()
            snapshot["input_accepted"] = event["accepted"]
            snapshot["qemu_input_result"] = result
            if advance is not None:
                snapshot["warning"] = "advance is ignored by the QEMU process backend"
            return snapshot

    def set_frontend_input_calibration(self, enabled: bool) -> dict[str, object]:
        self.args.frontend_input_calibration = bool(enabled)
        with self.lock:
            self._publish_snapshot_locked()
            return self.snapshot()

    def set_orientation(self, orientation: object) -> dict[str, object]:
        value = str(orientation or "").strip().lower()
        if value not in FRONTEND_ORIENTATIONS:
            return {"error": f"unsupported orientation {value!r}", "known": sorted(FRONTEND_ORIENTATIONS)}
        with self.lock:
            changed = getattr(self.args, "orientation", "rot180") != value
            self.args.orientation = value
            if changed:
                self.cached_frame_bytes = None
                self.cached_frame_seq = None
                self.cached_frame_time = 0.0
                latest = self._latest_qemu_raw_frame_locked()
                if latest is not None:
                    _seq, _captured_at, raw = latest
                    self.last_frame = self._frame_info_from_raw(raw, "qemu-frame-chardev")
            self._publish_snapshot_locked()
            snapshot = self.snapshot()
            snapshot["orientation_changed"] = changed
            return snapshot

    def _frontend_input_calibration_enabled(self) -> bool:
        return bool(getattr(self.args, "frontend_input_calibration", False))

    def _gdb_diagnostics_enabled(self) -> bool:
        return bool(getattr(self.args, "allow_gdb_diagnostics", False))

    def _gdb_diagnostics_disabled_error(self, op: str) -> dict[str, object]:
        return {
            "error": (
                f"{op} is disabled by default; pass --allow-gdb-diagnostics "
                "for explicit intrusive GDB diagnostics"
            )
        }

    def command(self, msg: dict[str, object]) -> dict[str, object]:
        op = str(msg.get("op", "status"))
        if op == "reset":
            return self.reset()
        if op in {"run-start", "run_start"}:
            return self.run_start(str(msg.get("name", "run")), int(msg.get("steps", 0)), int(msg.get("chunk", 100000)))
        if op == "stop":
            return self.stop()
        if op in {"force-stop", "force_stop"}:
            return self.force_stop()
        if op == "step":
            return self.step(int(msg.get("steps", 250000)))
        if op in {"set-orientation", "set_orientation", "orientation"}:
            return self.set_orientation(msg.get("orientation"))
        if op in {
            "frontend-input-calibration",
            "frontend_input_calibration",
            "set-frontend-input-calibration",
        }:
            enabled = self._coerce_optional_bool(msg.get("enabled"))
            if enabled is None:
                enabled = not self._frontend_input_calibration_enabled()
            return self.set_frontend_input_calibration(enabled)
        if op in {"set-nand-image", "set_nand_image", "select-nand-image", "select_nand_image"}:
            reset = self._coerce_optional_bool(msg.get("reset"))
            return self.set_nand_image(msg.get("path") or msg.get("image"), reset=reset is not False)
        if op in {"qemu-storage-service", "qemu_storage_service"}:
            return {"error": "qemu-storage-service is disabled; legacy Python/GDB storage hooks are not part of the bbk9588 default path"}
        if op in {
            "qemu-storage-trace",
            "qemu_storage_trace",
            "set-qemu-storage-trace",
            "set_qemu_storage_trace",
        }:
            if self.qemu_backend is None:
                return {"error": "QEMU backend is not initialized"}
            enabled = self._coerce_optional_bool(msg.get("enabled"))
            if enabled is None:
                return {"error": "enabled must be a boolean"}
            return self.qemu_backend.set_storage_trace(enabled)
        if op in {"qemu-task-trace", "qemu_task_trace", "qemu-fs-trace", "qemu_fs_trace", "qemu-event-loop-trace", "qemu_event_loop_trace", "qemu-resource-trace", "qemu_resource_trace"}:
            return {"error": f"{op} is disabled; use QEMU machine/device instrumentation instead of Python/GDB services"}
        if op in {"qemu-read-memory", "qemu_read_memory"}:
            if self.qemu_backend is None:
                return {"error": "QEMU backend is not initialized"}
            addr = int(str(msg.get("addr", 0)), 0)
            size = max(0, min(int(msg.get("size", 0x80)), 0x1000))
            if size == 0:
                return {"addr": f"0x{addr & 0xFFFFFFFF:08x}", "size": 0, "hex": ""}
            try:
                data = self.qemu_backend.read_guest_memory(addr, size)
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            out: dict[str, object] = {"addr": f"0x{addr & 0xFFFFFFFF:08x}", "size": len(data), "hex": data.hex()}
            raw = data.split(b"\x00", 1)[0]
            for encoding in ("gbk", "ascii"):
                try:
                    out[encoding] = raw.decode(encoding)
                except UnicodeDecodeError:
                    pass
            return out
        if op in {"qemu-read-physical-memory", "qemu_read_physical_memory"}:
            if self.qemu_backend is None:
                return {"error": "QEMU backend is not initialized"}
            addr = int(str(msg.get("addr", 0)), 0)
            size = max(0, min(int(msg.get("size", 0x80)), 0x1000))
            try:
                data = self.qemu_backend.read_physical_memory(addr, size)
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            return {
                "addr": f"0x{addr & 0xFFFFFFFF:08x}",
                "size": len(data),
                "hex": data.hex(),
            }
        if op in {"qemu-watch-write", "qemu_watch_write"}:
            if not self._gdb_diagnostics_enabled():
                return self._gdb_diagnostics_disabled_error(op)
            if self.qemu_backend is None:
                return {"error": "QEMU backend is not initialized"}
            addr = int(str(msg.get("addr", 0)), 0)
            size = int(msg.get("size", 4))
            timeout = float(msg.get("timeout", 10.0))
            trigger_touch = None
            values = msg.get("trigger_touch")
            if isinstance(values, (list, tuple)) and len(values) >= 2:
                trigger_touch = (int(values[0]), int(values[1]))
            ignore_pcs = ()
            raw_ignore = msg.get("ignore_pcs")
            if isinstance(raw_ignore, (list, tuple)):
                ignore_pcs = tuple(int(str(value), 0) for value in raw_ignore)
            return self.qemu_backend.watch_guest_write_once(
                addr,
                size,
                timeout,
                trigger_touch,
                float(msg.get("trigger_hold_seconds", 0.0)),
                ignore_pcs,
                int(msg.get("max_hits", 1)),
            )
        if op in {"qemu-trace-breakpoints", "qemu_trace_breakpoints"}:
            if not self._gdb_diagnostics_enabled():
                return self._gdb_diagnostics_disabled_error(op)
            if self.qemu_backend is None:
                return {"error": "QEMU backend is not initialized"}
            pcs_raw = msg.get("pcs", [])
            if not isinstance(pcs_raw, (list, tuple)):
                return {"error": "pcs must be a list"}
            pcs = tuple(int(str(value), 0) for value in pcs_raw)
            trigger_touch = None
            values = msg.get("trigger_touch")
            if isinstance(values, (list, tuple)) and len(values) >= 2:
                trigger_touch = (int(values[0]), int(values[1]))
            sample_rect = None
            rect = msg.get("sample_rect")
            if isinstance(rect, (list, tuple)) and len(rect) >= 4:
                sample_rect = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
            return self.qemu_backend.trace_guest_breakpoints_once(
                pcs,
                timeout=float(msg.get("timeout", 5.0)),
                max_hits=int(msg.get("max_hits", 32)),
                trigger_touch=trigger_touch,
                trigger_hold_seconds=float(msg.get("trigger_hold_seconds", 0.0)),
                sample_rect=sample_rect,
                dedupe_blits=self._coerce_optional_bool(msg.get("dedupe_blits")) is True,
            )
        if op == "key":
            include_snapshot = self._coerce_optional_bool(msg.get("reply")) is not False
            out = self.key(
                int(msg.get("code", 0)),
                self._coerce_optional_bool(msg.get("down")) is not False,
                self._coerce_optional_bool(msg.get("advance")),
                include_snapshot=include_snapshot,
                input_session=msg.get("input_session"),
                source=msg.get("source"),
            )
            if include_snapshot and self._coerce_optional_bool(msg.get("run")) is True:
                run_status = self.run_start("qemu-input", 0, 0)
                for key_name in ("input_accepted", "qemu_input_result", "warning"):
                    if key_name in out:
                        run_status[key_name] = out[key_name]
                return run_status
            return out
        if op == "touch":
            include_snapshot = self._coerce_optional_bool(msg.get("reply")) is not False
            x, y = self._touch_coordinates_from_message(msg)
            out = self.touch(
                x,
                y,
                self._coerce_optional_bool(msg.get("down")) is not False,
                self._coerce_optional_bool(msg.get("advance")),
                include_snapshot=include_snapshot,
            )
            if isinstance(self.last_input_event, dict):
                self.last_input_event["source"] = msg.get("source") or "message"
                self.last_input_event["phase"] = msg.get("phase")
                if "display_x" in msg or "display_y" in msg:
                    self.last_input_event["display_x"] = int(msg.get("display_x", 0))
                    self.last_input_event["display_y"] = int(msg.get("display_y", 0))
                    self.last_input_event["display_width"] = int(msg.get("display_width", 240))
                    self.last_input_event["display_height"] = int(msg.get("display_height", 320))
            if include_snapshot and self._coerce_optional_bool(msg.get("run")) is True:
                run_status = self.run_start("qemu-input", 0, 0)
                for key_name in ("input_accepted", "qemu_input_result", "warning"):
                    if key_name in out:
                        run_status[key_name] = out[key_name]
                return run_status
            return out
        return self.snapshot()

    def logs(self, limit: int = 512) -> dict[str, object]:
        backend = self.qemu_backend
        if backend is None:
            return {"count": 0, "limit": limit, "events": []}
        snap = self._backend_snapshot(backend, refresh=False)
        lines = [
            *[{"stream": "stdout", "text": line} for line in snap.get("stdout_tail", [])],
            *[{"stream": "stderr", "text": line} for line in snap.get("stderr_tail", [])],
        ]
        limit = max(1, min(5000, int(limit)))
        return {"count": len(lines), "limit": limit, "events": lines[-limit:]}

    def clear_logs(self) -> dict[str, object]:
        with self.lock:
            backend = self.qemu_backend
            removed = 0
            if backend is not None:
                snap = self._backend_snapshot(backend, refresh=False)
                removed = len(snap.get("stdout_tail", [])) + len(snap.get("stderr_tail", []))
                backend.stdout_tail.clear()
                backend.stderr_tail.clear()
            self._publish_snapshot_locked()
            return {"cleared": removed}

    def _frame_info_from_raw(self, raw: bytes, source: str) -> dict[str, object]:
        info, _rgb = rgb565_raw_to_info_rgb(
            raw,
            LIVE_FRAMEBUFFER_ADDR,
            LIVE_FRAMEBUFFER_OFFSET_BYTES,
            LIVE_FRAMEBUFFER_WIDTH,
            LIVE_FRAMEBUFFER_HEIGHT,
            LIVE_FRAMEBUFFER_STRIDE_PIXELS,
            LIVE_FRAMEBUFFER_FORMAT,
            getattr(self.args, "orientation", "rot180"),
        )
        info["backend"] = "qemu"
        info["source"] = source
        info["available"] = True
        return info

    def dump_frame(self) -> bytes:
        now = time.time()
        with self.lock:
            self._record_screen_png_dump_locked(now)
            cached = self.cached_frame_bytes
            cached_frame_time = self.cached_frame_time
        if cached is not None and now - cached_frame_time < 0.25:
            return cached
        backend: QemuProcessBackend
        with self.lock:
            backend = self._ensure_qemu_started_locked()
            latest = self._latest_qemu_raw_frame_locked()
            if latest is not None:
                seq, captured_at, raw = latest
                if self.cached_frame_bytes is not None and self.cached_frame_seq == seq:
                    return self.cached_frame_bytes
                frame = self._cache_png_raw_frame_locked(raw, "qemu-frame-chardev", captured_at, seq)
                self._notify_frontend_activity()
                return frame
            if cached is not None:
                return cached

        if not self.frame_io_lock.acquire(blocking=False):
            with self.lock:
                if self.cached_frame_bytes is not None:
                    return self.cached_frame_bytes
                frame = self._placeholder_png_frame_locked("frame read already in progress")
                self._notify_frontend_activity()
                return frame
        try:
            try:
                try:
                    raw, source = backend.read_display_rgb565_frame()
                except Exception:
                    raw = backend.read_physical_memory(LIVE_FRAMEBUFFER_ADDR & 0x1FFFFFFF, LIVE_FRAMEBUFFER_RAW_BYTES)
                    source = "hmp-pmemsave"
            except Exception as exc:
                with self.lock:
                    frame = self._placeholder_png_frame_locked(f"{type(exc).__name__}: {exc}")
                    self._notify_frontend_activity()
                    return frame
            with self.lock:
                frame = self._cache_png_raw_frame_locked(raw, source)
                self._notify_frontend_activity()
                return frame
        finally:
            self.frame_io_lock.release()

    def dump_qemu_guest_rgb565(
        self,
        addr: int,
        *,
        width: int = 240,
        height: int = 320,
        stride_pixels: int | None = None,
        orientation: str = "raw",
    ) -> bytes:
        if width <= 0 or height <= 0 or width > 1024 or height > 1024:
            raise ValueError("invalid RGB565 dimensions")
        stride = stride_pixels if stride_pixels is not None else width
        if stride < width or stride > 4096:
            raise ValueError("invalid RGB565 stride")
        if orientation not in {"raw", "rot180", "cw90", "ccw90", "hflip", "vflip"}:
            raise ValueError("invalid RGB565 orientation")
        with self.lock:
            backend = self._ensure_qemu_started_locked()
            raw = backend.read_guest_memory(int(addr), stride * height * 2)
        info, rgb = rgb565_raw_to_info_rgb(raw, int(addr), 0, width, height, stride, "rgb565", orientation)
        return png_bytes_from_rgb(int(info["output_width"]), int(info["output_height"]), rgb)

    def dump_qemu_guest_memory(self, addr: int, size: int) -> bytes:
        if size <= 0 or size > 4 * 1024 * 1024:
            raise ValueError("invalid memory dump size")
        with self.lock:
            backend = self._ensure_qemu_started_locked()
            return backend.read_guest_memory(int(addr), int(size))

    def _pending_touch_count_locked(self) -> int:
        with self.input_lock:
            return len(self.pending_touches)

    def _pending_key_count_locked(self) -> int:
        with self.input_lock:
            return len(self.pending_keys)

    def _build_snapshot_locked(self, *, detail: str = "compact") -> dict[str, object]:
        self._expire_frontend_key_leases_locked()
        backend = self.qemu_backend
        if backend is not None and detail in {"full", "traces"}:
            self._apply_frontend_input_calibration_locked(backend)
        qemu = {} if backend is None else self._backend_snapshot(backend, refresh=detail in {"full", "traces"})
        qemu_sample = qemu.get("register_sample") if isinstance(qemu.get("register_sample"), dict) else {}
        qemu_pc = qemu.get("pc") or (qemu_sample.get("pc") if isinstance(qemu_sample, dict) else None)
        qemu_cp0 = qemu.get("cp0") or (qemu_sample.get("cp0") if isinstance(qemu_sample, dict) else None)
        qemu_pc_classification = qemu.get("pc_classification") if isinstance(qemu.get("pc_classification"), dict) else classify_guest_pc(qemu_pc)
        qemu_pc_region = qemu_pc_classification.get("region") if isinstance(qemu_pc_classification, dict) else None
        active = bool(qemu.get("running"))
        stop_detail = self._stop_detail(active, qemu)
        now = time.time()
        reset_elapsed = max(0.0, now - self.reset_at)
        frontend_performance = self._frontend_performance_snapshot_locked(now, reset_elapsed)
        elapsed = qemu.get("elapsed_seconds")
        job = {
            "name": self.job_name or "qemu",
            "mode": "process",
            "status": "running" if active else "stopped",
            "total_steps": self.job_total_steps,
            "done_steps": self.job_done_steps,
            "requested_done_steps": self.job_done_steps,
            "chunk_steps": self.job_chunk_steps,
            "last_slice_steps": self.job_last_slice_steps,
            "last_slice_timed_out": self.job_last_slice_timed_out,
            "observed_insn_delta": 0,
            "active": active,
            "started_at": self.job_started_at,
            "finished_at": self.job_finished_at,
            "elapsed_seconds": elapsed,
            "steps_per_second": None,
            "observed_steps_per_second": None,
            "requested_steps_per_second": None,
        }
        effective_nand_image = self._default_nand_image()
        snapshot: dict[str, object] = {
            "backend": "qemu",
            "running": active,
            "boot_mode": getattr(self.args, "boot_mode", "nand"),
            "nand_image": str(effective_nand_image.resolve()) if effective_nand_image is not None else "",
            "nand_write_mode": "direct" if effective_nand_image is not None else "none",
            "nand_legacy_checkpoint_migrated": getattr(
                self, "nand_legacy_checkpoint_migrated", None
            ),
            "orientation": getattr(self.args, "orientation", "rot180"),
            "reset_at": self.reset_at,
            "reset_elapsed_seconds": reset_elapsed,
            "emulator_elapsed_seconds": elapsed if isinstance(elapsed, (int, float)) else reset_elapsed,
            "run_started_at": self.job_started_at,
            "run_finished_at": self.job_finished_at,
            "run_elapsed_seconds": elapsed,
            "run_steps_per_second": None,
            "run_requested_steps_per_second": None,
            "legacy_python_hooks": {
                "enabled": False,
                "storage_hook_enabled": False,
                "resource_hook_enabled": False,
                "reason": "Legacy Python/GDB hooks are disabled; default bbk9588 behavior is modeled in QEMU.",
            },
            "frontend_input_calibration": self._frontend_input_calibration_enabled(),
            "frontend_input_calibration_stage": self.frontend_input_calibration_stage,
            "frontend_input_calibration_stage_label": FRONTEND_INPUT_CALIBRATION_STAGE_LABELS.get(
                self.frontend_input_calibration_stage,
                str(self.frontend_input_calibration_stage),
            ),
            "pending_touches": self._pending_touch_count_locked(),
            "pending_keys": self._pending_key_count_locked(),
            "frontend_active_keys": sorted(self.frontend_active_key_codes),
            "frontend_key_leases": len(self.frontend_key_leases),
            "frontend_key_lease_expirations": self.frontend_key_lease_expiration_count,
            "safe_shutdown": {
                "state": self.safe_shutdown_state,
                "started_at": self.safe_shutdown_started_at,
                "finished_at": self.safe_shutdown_finished_at,
                "error": self.safe_shutdown_error,
            },
            "lifecycle_notice": getattr(self, "lifecycle_notice", None),
            "job": job,
            "input_worker_pending": False,
            "input_wake_count": self.input_wake_count,
            "last_input_event": self.last_input_event,
            "stop_reason": None if stop_detail is None else stop_detail.get("code"),
            "stop_detail": stop_detail,
            "crash_snapshot": self.crash_snapshot,
            "insn_count": 0,
            "pc": qemu_pc,
            "last_pc": qemu_pc,
            "cp0": qemu_cp0,
            "qemu_pc_region": qemu_pc_region,
            "idle_loop_hits": 0,
            "app_idle_loop_hits": 0,
            "events": [],
            "invalid": [],
            "recoveries": [],
            "scheduler": {},
            "framebuffer": self.last_frame,
            "framebuffer_dirty_seq": 0,
            "framebuffer_dirty_last": None,
            "queued_frames": 0,
            "frontend_performance": frontend_performance,
            "audio_transport": self._audio_transport_snapshot(),
            "frame_push": {
                "min_interval": self.frame_push_min_interval,
                "hook_count": self.frame_push_hook_count,
                "queued_count": self.frame_push_queued_count,
                "throttle_count": self.frame_push_throttle_count,
                "deferred_count": self.frame_push_deferred_count,
                "replace_count": self.frame_push_replace_count,
                "deferred_due_at": None,
                "drop_count": self.frame_push_drop_count,
                "error_count": self.frame_push_error_count,
                "source_lag_ms": (
                    None
                    if self.frame_push_last_source_lag_ms is None
                    else round(self.frame_push_last_source_lag_ms, 2)
                ),
                "max_source_lag_ms": round(self.frame_push_max_source_lag_ms, 2),
                "info_min_interval": self.frame_info_min_interval,
                "info_update_count": self.frame_info_update_count,
                "last_push_at": self.frame_push_last_time,
                "ws_sent_count": self.ws_frame_sent_count,
                "ws_sent_bytes": self.ws_frame_sent_bytes,
                "ws_last_seq": self.ws_frame_last_seq,
                "ws_last_kind": self.ws_frame_last_kind,
                "ws_last_bytes": self.ws_frame_last_bytes,
                "ws_last_sent_at": self.ws_frame_last_sent_at,
                "ws_fps": frontend_performance.get("websocket_fps"),
                "ws_average_fps": frontend_performance.get("websocket_average_fps"),
                "ws_transport_fps": frontend_performance.get("websocket_transport_fps"),
                "ws_connections": self.ws_connection_count,
                "ws_connection_peak": self.ws_connection_peak,
                "screen_png_count": self.screen_png_count,
                "screen_png_fps": frontend_performance.get("screen_png_fps"),
            },
            "qemu": qemu,
            "qemu_pc_classification": qemu_pc_classification,
            "ws": {
                "command_count": self.ws_command_count,
                "last_op": self.ws_last_command_op,
                "last_seq": self.ws_last_command_seq,
                "last_command_at": self.ws_last_command_at,
                "reader_alive": self.ws_reader_alive,
                "reader_heartbeat": self.ws_reader_heartbeat,
            },
            "status_cached_at": time.time(),
        }
        if detail in {"full", "traces"}:
            snapshot["detail"] = detail
            if backend is not None:
                snapshot["event_queue"] = backend.guest_queue_snapshot(0x80473F6C)
                snapshot["display_event_queue"] = backend.guest_display_queue_snapshot(0x80825840)
                snapshot["guest_gui_state"] = backend.guest_gui_state_snapshot()
                snapshot["qemu_scheduler_state"] = backend.guest_scheduler_state_snapshot()
                snapshot["guest_touch_device"] = backend.guest_touch_device_snapshot()
                snapshot["guest_runtime_tables"] = backend.guest_runtime_table_snapshot()
                snapshot["guest_display_surface"] = backend.guest_display_surface_snapshot()
                if detail == "traces":
                    dmac_trace_snapshot = getattr(backend, "dmac_trace_snapshot", None)
                    if callable(dmac_trace_snapshot):
                        snapshot["qemu_dmac_trace"] = dmac_trace_snapshot()
                    snapshot["guest_surface_trace"] = backend.guest_surface_trace_snapshot()
                    snapshot["guest_storage_trace"] = backend.guest_storage_trace_snapshot()
                    snapshot["guest_msc_trace"] = backend.guest_msc_trace_snapshot()
                    snapshot["guest_fs_probe_trace"] = backend.guest_fs_probe_trace_snapshot()
                    snapshot["guest_progress_trace"] = backend.guest_progress_trace_snapshot()
                snapshot["recent_event_queue_snapshots"] = []
            snapshot["qemu_frontend_input_calibration_log"] = list(self.qemu_frontend_input_calibration_log)
            snapshot["qemu_limitations"] = [
                "QEMU bbk9588 models the default process, frame chardev, input chardev, timers, interrupts, GPIO/SADC touch state, and NAND-backed storage paths.",
                "Remaining work belongs in the QEMU SoC model, not in Python firmware hooks.",
            ]
        return snapshot

    def _stop_detail(
        self,
        active: bool,
        qemu: dict[str, object],
    ) -> dict[str, object] | None:
        if active:
            return None
        reason = qemu.get("exit_reason")
        error = self.last_error or qemu.get("last_error")
        if reason is None and error is not None:
            reason = "start-error" if qemu.get("returncode") is None else "process-error"
        if reason is None:
            return None
        expected = reason in {"guest-shutdown", "guest-reset", "user-stop", "process-exit"}
        return {
            "code": str(reason),
            "returncode": qemu.get("returncode"),
            "occurred_at": qemu.get("finished_at") or self.job_finished_at,
            "expected": expected,
            "error": None if expected else error,
        }

    def _publish_snapshot_locked(self) -> None:
        with self.status_lock:
            self.cached_status = self._build_snapshot_locked()
        self._notify_frontend_activity()

    def snapshot(self, *, detail: str = "compact") -> dict[str, object]:
        if detail == "compact":
            acquired = self.lock.acquire(blocking=False)
            if not acquired:
                with self.status_lock:
                    if self.cached_status:
                        return dict(self.cached_status)
                with self.lock:
                    snapshot = self._build_snapshot_locked(detail=detail)
            else:
                try:
                    snapshot = self._build_snapshot_locked(detail=detail)
                finally:
                    self.lock.release()
        else:
            with self.lock:
                snapshot = self._build_snapshot_locked(detail=detail)
        with self.status_lock:
            self.cached_status = dict(snapshot)
        return snapshot

    def _apply_frontend_input_calibration_locked(self, backend: QemuProcessBackend) -> None:
        """Feed cold-boot calibration touches through the QEMU input chardev."""

        if not self._frontend_input_calibration_enabled() or getattr(self.args, "boot_mode", "nand") not in {"nand", "c200", "uboot"}:
            return
        qemu = self._backend_snapshot(backend, refresh=False)
        if not qemu.get("running"):
            return
        if self.frontend_input_calibration_stage >= 12:
            return
        now = time.time()
        if now - self.qemu_frontend_input_calibration_last_action_at < 0.45:
            return
        pc_s = qemu.get("pc")
        try:
            pc = int(str(pc_s), 16)
        except Exception:
            pc = 0
        qemu_sample = qemu.get("register_sample") if isinstance(qemu.get("register_sample"), dict) else {}
        ra_s = qemu_sample.get("ra") if isinstance(qemu_sample, dict) else None
        try:
            ra = int(str(ra_s), 16)
        except Exception:
            ra = 0

        point_count = len(FRONTEND_INPUT_CALIBRATION_TARGETS)
        pc_unknown = pc == 0 and ra == 0
        in_touch_boot = 0x80017B74 <= pc <= 0x80019300 or 0x80017B74 <= ra <= 0x80019300
        in_uboot = 0x80900000 <= pc < 0x80A00000 or 0x80900000 <= ra < 0x80A00000
        in_c200 = 0x80000000 <= pc < 0x80900000 or 0x80000000 <= ra < 0x80900000

        if in_uboot or (not pc_unknown and not in_c200 and not in_touch_boot):
            return

        reset_at = float(getattr(self, "reset_at", now) or now)
        if pc_unknown and now - reset_at < FRONTEND_INPUT_CALIBRATION_PC_GRACE_SECONDS:
            return

        if self.frontend_input_calibration_stage < point_count * 2:
            if not pc_unknown and not in_touch_boot and pc != 0:
                try:
                    gui = backend.guest_gui_state_snapshot()
                except Exception:
                    return
                active = int(str(gui.get("active_object_80474048") or "0x0"), 16)
                if active == 0x80959670 or bool(gui.get("active_object_ready")):
                    self.frontend_input_calibration_stage = 12
                    self.frontend_input_calibration_last_stage_step += 1
                    self.qemu_frontend_input_calibration_last_action_at = now
                    self.qemu_frontend_input_calibration_log.append(
                        {
                            "event": "qemu-frontend-input-calibration-complete",
                            "pc": f"0x{pc:08x}",
                            "ra": f"0x{ra:08x}",
                            "active": gui.get("active_object_80474048"),
                            "reason": "main-menu-already-active",
                        }
                    )
                    del self.qemu_frontend_input_calibration_log[:-16]
                return
            point_index = self.frontend_input_calibration_stage // 2
            down = self.frontend_input_calibration_stage % 2 == 0
            x, y = FRONTEND_INPUT_CALIBRATION_TARGETS[point_index]
            result = backend.apply_touch_state(x, y, down)
            self.frontend_input_calibration_stage += 1
            self.frontend_input_calibration_last_stage_step += 1
            self.qemu_frontend_input_calibration_last_action_at = now
            self.qemu_frontend_input_calibration_log.append(
                {
                    "event": "qemu-frontend-input-calibration-touch",
                    "stage": self.frontend_input_calibration_stage,
                    "point": point_index + 1,
                    "down": down,
                    "x": x,
                    "y": y,
                    "pc": f"0x{pc:08x}",
                    "ra": f"0x{ra:08x}",
                    "result": result,
                }
            )
            del self.qemu_frontend_input_calibration_log[:-16]
            return

        gui: dict[str, object] = {}
        try:
            gui = backend.guest_gui_state_snapshot()
        except Exception as exc:
            gui = {"error": f"{type(exc).__name__}: {exc}"}

        if self.frontend_input_calibration_stage >= point_count * 2:
            if "error" in gui:
                self.qemu_frontend_input_calibration_last_action_at = now
                self.qemu_frontend_input_calibration_log.append(
                    {
                        "event": "qemu-frontend-input-calibration-status-deferred",
                        "stage": self.frontend_input_calibration_stage,
                        "pc": f"0x{pc:08x}",
                        "ra": f"0x{ra:08x}",
                        "error": gui.get("error"),
                    }
                )
                del self.qemu_frontend_input_calibration_log[:-16]
                return
            active = int(str(gui.get("active_object_80474048") or "0x0"), 16)
            active_ready = bool(gui.get("active_object_ready"))
            modal = int(str(gui.get("modal_804a65c0") or "0x0"), 16)
            if active == 0x80959670 or active_ready:
                self.frontend_input_calibration_stage = 12
                self.frontend_input_calibration_last_stage_step += 1
                self.qemu_frontend_input_calibration_last_action_at = now
                self.qemu_frontend_input_calibration_log.append(
                    {
                        "event": "qemu-frontend-input-calibration-complete",
                        "pc": f"0x{pc:08x}",
                        "ra": f"0x{ra:08x}",
                        "active": gui.get("active_object_80474048"),
                        "reason": "main-menu-active" if active == 0x80959670 else "calibration-touches-complete",
                    }
                )
                del self.qemu_frontend_input_calibration_log[:-16]
                return
            if modal and self.frontend_input_calibration_stage == point_count * 2:
                result = backend.apply_touch_state(FRONTEND_INPUT_DIALOG_X, FRONTEND_INPUT_DIALOG_Y, True)
                self.frontend_input_calibration_stage = point_count * 2 + 1
                self.frontend_input_calibration_last_stage_step += 1
                self.qemu_frontend_input_calibration_last_action_at = now
                self.qemu_frontend_input_calibration_log.append(
                    {
                        "event": "qemu-frontend-input-dialog-touch",
                        "stage": self.frontend_input_calibration_stage,
                        "down": True,
                        "x": FRONTEND_INPUT_DIALOG_X,
                        "y": FRONTEND_INPUT_DIALOG_Y,
                        "pc": f"0x{pc:08x}",
                        "modal": f"0x{modal:08x}",
                        "result": result,
                    }
                )
                del self.qemu_frontend_input_calibration_log[:-16]
                return
            if modal and self.frontend_input_calibration_stage == point_count * 2 + 1:
                result = backend.apply_touch_state(FRONTEND_INPUT_DIALOG_X, FRONTEND_INPUT_DIALOG_Y, False)
                self.frontend_input_calibration_stage = point_count * 2 + 2
                self.frontend_input_calibration_last_stage_step += 1
                self.qemu_frontend_input_calibration_last_action_at = now
                self.qemu_frontend_input_calibration_log.append(
                    {
                        "event": "qemu-frontend-input-dialog-touch",
                        "stage": self.frontend_input_calibration_stage,
                        "down": False,
                        "x": FRONTEND_INPUT_DIALOG_X,
                        "y": FRONTEND_INPUT_DIALOG_Y,
                        "pc": f"0x{pc:08x}",
                        "modal": f"0x{modal:08x}",
                        "result": result,
                    }
                )
                del self.qemu_frontend_input_calibration_log[:-16]
            return

        return
