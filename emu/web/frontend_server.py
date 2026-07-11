"""HTTP/WebSocket request handler for the BBK 9588 frontend."""

from __future__ import annotations

import json
import mimetypes
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from emu.web.frontend_ws import WebSocketFrameReader, encode_ws_frame, websocket_accept_key


class FrontendHandler(BaseHTTPRequestHandler):
    state: object
    html: str = ""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.close_connection = True
        self.connection.settimeout(1.0)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: object, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _download(self, name: str, body: bytes) -> None:
        self.close_connection = True
        self.connection.settimeout(1.0)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _ws_send_frame(self, opcode: int, payload: bytes) -> None:
        self.connection.sendall(encode_ws_frame(opcode, payload))

    def _ws_send_json(self, data: object) -> None:
        self._ws_send_frame(0x1, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _ws_send_frame_payload(self, allow_cached: bool = True, allow_dump: bool = True) -> bool:
        popper = getattr(self.state, "pop_queued_ws_frame", None)
        frame = popper() if popper is not None else self.state.pop_queued_frame()
        if frame is None and allow_cached:
            cached = getattr(self.state, "cached_ws_frame", None)
            frame = cached() if cached is not None else None
        if frame is None and allow_cached:
            frame = self.state.cached_frame()
        if frame is None and allow_dump:
            dumper = getattr(self.state, "dump_ws_frame", None)
            frame = dumper() if dumper is not None else self.state.dump_frame()
        if frame is None:
            return False
        self._ws_send_frame(0x2, frame)
        recorder = getattr(self.state, "record_ws_frame_sent", None)
        if recorder is not None:
            recorder(frame)
        return True

    def _ws_send_queued_frame_payload(self) -> bool:
        popper = getattr(self.state, "pop_latest_queued_ws_frame", None)
        frame = popper() if popper is not None else self.state.pop_latest_queued_frame()
        if frame is None:
            return False
        self._ws_send_frame(0x2, frame)
        recorder = getattr(self.state, "record_ws_frame_sent", None)
        if recorder is not None:
            recorder(frame)
        return True

    def _ws_send_latest_frame_payload(self, last_seq: int | None) -> tuple[bool, int | None]:
        getter = getattr(self.state, "latest_ws_frame_after", None)
        if getter is None:
            return self._ws_send_queued_frame_payload(), last_seq
        latest = getter(last_seq)
        if latest is None:
            return False, last_seq
        seq, frame = latest
        self._ws_send_frame(0x2, frame)
        recorder = getattr(self.state, "record_ws_frame_sent", None)
        if recorder is not None:
            recorder(frame)
        return True, int(seq)

    def _ws_response_for_text(self, text: str) -> dict[str, object] | None:
        if text is None:
            return None
        if not text:
            return None
        try:
            msg = json.loads(text)
            if isinstance(msg, dict):
                op = str(msg.get("op", "status"))
                command_seq = msg.get("command_seq")
                recorder = getattr(self.state, "record_ws_command", None)
                if recorder is not None:
                    recorder(op, command_seq)
                response = self.state.command(msg)
                if isinstance(response, dict):
                    response = dict(response)
                    response["_ws_op"] = op
                    if command_seq is not None:
                        response["_ws_command_seq"] = command_seq
                return response
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return None

    def _handle_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, b"missing websocket key", "text/plain")
            return
        accept = websocket_accept_key(key)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        # The same socket is used by the reader thread and the frame sender.
        # Raw RGB565 frames are 153 KiB, so a 250 ms socket timeout can close a
        # healthy WS connection if the browser is briefly busy. Keep the
        # condition-variable loop responsive and give sendall enough room.
        self.connection.settimeout(2.0)
        alive = threading.Event()
        alive.set()
        connection_registrar = getattr(self.state, "register_ws_connection", None)
        connection_unregistrar = getattr(self.state, "unregister_ws_connection", None)
        if connection_registrar is not None:
            connection_registrar()
        reader_alive_setter = getattr(self.state, "set_ws_reader_alive", None)
        if reader_alive_setter is not None:
            reader_alive_setter(True)
        pending_lock = threading.Lock()
        pending_responses: deque[dict[str, object]] = deque()
        activity_seq_getter = getattr(self.state, "frontend_activity_sequence", None)
        activity_waiter = getattr(self.state, "wait_for_frontend_activity", None)
        activity_notifier = getattr(self.state, "notify_frontend_activity", None)
        deferred_delay_getter = getattr(self.state, "seconds_until_deferred_frame", None)
        activity_seq = activity_seq_getter() if activity_seq_getter is not None else 0

        def queue_response(response: dict[str, object] | None) -> None:
            if response is None:
                return
            with pending_lock:
                pending_responses.append(response)
            if activity_notifier is not None:
                activity_notifier()

        def reader() -> None:
            frame_reader = WebSocketFrameReader(self.connection)
            try:
                while alive.is_set():
                    try:
                        text = frame_reader.recv_text()
                    except TimeoutError:
                        if reader_alive_setter is not None:
                            reader_alive_setter(True)
                        continue
                    except OSError:
                        break
                    if text is None:
                        break
                    queue_response(self._ws_response_for_text(text))
            finally:
                alive.clear()
                if reader_alive_setter is not None:
                    reader_alive_setter(False)
                if activity_notifier is not None:
                    activity_notifier()

        reader_thread = threading.Thread(target=reader, name="hwemu-ws-reader", daemon=True)
        reader_thread.start()

        def pop_response() -> dict[str, object] | None:
            with pending_lock:
                return pending_responses.popleft() if pending_responses else None

        last_status_push = 0.0
        last_frame_seq: int | None = None
        try:
            self._ws_send_json(self.state.snapshot())
            last_status_push = time.time()
            frame_sent, last_frame_seq = self._ws_send_latest_frame_payload(last_frame_seq)
            if not frame_sent:
                self._ws_send_frame_payload(allow_cached=True, allow_dump=not self.state.worker_active())
            while alive.is_set() or pending_responses:
                now = time.time()
                response_sent = False
                for _ in range(32):
                    response = pop_response()
                    if response is None:
                        break
                    self._ws_send_json(response)
                    last_status_push = now
                    response_sent = True
                if response_sent:
                    continue
                frame_sent, last_frame_seq = self._ws_send_latest_frame_payload(last_frame_seq)
                if frame_sent:
                    if now - last_status_push >= 0.5:
                        self._ws_send_json(self.state.snapshot())
                        last_status_push = now
                elif now - last_status_push >= 0.5:
                    self._ws_send_json(self.state.snapshot())
                    last_status_push = now
                else:
                    status_delay = max(0.0, 0.5 - (time.time() - last_status_push))
                    wait_timeout = min(0.25, status_delay)
                    if deferred_delay_getter is not None:
                        deferred_delay = deferred_delay_getter()
                        if deferred_delay is not None:
                            wait_timeout = min(wait_timeout, deferred_delay)
                    if activity_waiter is not None:
                        activity_seq = activity_waiter(activity_seq, wait_timeout)
                    elif wait_timeout > 0:
                        time.sleep(wait_timeout)
        except OSError:
            pass
        finally:
            alive.clear()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            reader_thread.join(timeout=0.5)
            if reader_alive_setter is not None:
                reader_alive_setter(False)
            if connection_unregistrar is not None:
                connection_unregistrar()

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(200, self.html.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/ws":
                self._handle_ws()
            elif parsed.path == "/api/status":
                detail = parse_qs(parsed.query).get("detail", ["compact"])[0]
                if detail in {"full", "traces"}:
                    self._json(self.state.snapshot(detail=detail))
                else:
                    self._json(self.state.snapshot())
            elif parsed.path == "/api/images":
                self._json(self.state.nand_image_catalog())
            elif parsed.path == "/api/files":
                directory = parse_qs(parsed.query).get("path", ["/"])[0]
                self._json(self.state.nand_files_list(directory))
            elif parsed.path == "/api/files/export":
                file_path = parse_qs(parsed.query).get("path", [""])[0]
                if not file_path:
                    raise ValueError("missing NAND file path")
                name, data = self.state.nand_file_export(file_path)
                self._download(name, data)
            elif parsed.path == "/api/logs":
                limit = int(parse_qs(parsed.query).get("limit", ["512"])[0])
                self._json(self.state.logs(limit))
            elif parsed.path == "/screen.png":
                self._send(200, self.state.dump_frame(), "image/png")
            elif parsed.path == "/debug/rgb565.png":
                qs = parse_qs(parsed.query)
                addr_text = qs.get("addr", [""])[0]
                if not addr_text:
                    raise ValueError("missing addr")
                addr = int(addr_text, 0)
                width = int(qs.get("w", ["240"])[0])
                height = int(qs.get("h", ["320"])[0])
                stride = int(qs.get("stride", [str(width)])[0])
                orientation = qs.get("orientation", ["raw"])[0]
                self._send(
                    200,
                    self.state.dump_qemu_guest_rgb565(
                        addr,
                        width=width,
                        height=height,
                        stride_pixels=stride,
                        orientation=orientation,
                    ),
                    "image/png",
                )
            elif parsed.path == "/debug/mem.bin":
                qs = parse_qs(parsed.query)
                addr_text = qs.get("addr", [""])[0]
                if not addr_text:
                    raise ValueError("missing addr")
                addr = int(addr_text, 0)
                size = int(qs.get("size", ["0"])[0])
                if size <= 0 or size > 4 * 1024 * 1024:
                    raise ValueError("invalid size")
                self._send(200, self.state.dump_qemu_guest_memory(addr, size), "application/octet-stream")
            else:
                ctype = mimetypes.guess_type(parsed.path)[0] or "text/plain"
                self._send(404, b"not found", ctype)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
                return

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/api/reset":
                self._json(self.state.reset())
            elif parsed.path == "/api/command":
                self._json(self.state.command(self._read_json_body()))
            elif parsed.path == "/api/files/mkdir":
                body = self._read_json_body()
                self._json(self.state.nand_files_mkdir(body.get("path", "/"), body.get("name")))
            elif parsed.path == "/api/files/rename":
                body = self._read_json_body()
                self._json(self.state.nand_files_rename(body.get("path"), body.get("name")))
            elif parsed.path == "/api/files/delete":
                body = self._read_json_body()
                self._json(self.state.nand_files_delete(body.get("path")))
            elif parsed.path == "/api/files/import":
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length < 0 or length > 128 * 1024 * 1024:
                    raise ValueError("invalid NAND upload size")
                data = self.rfile.read(length) if length else b""
                directory = qs.get("path", ["/"])[0]
                name = qs.get("name", [""])[0]
                self._json(self.state.nand_files_import(directory, name, data))
            elif parsed.path == "/api/boot":
                self._json(self.state.boot())
            elif parsed.path == "/api/checkpoint":
                path = qs.get("path", [""])[0]
                if not path:
                    raise ValueError("missing checkpoint path")
                self._json(self.state.save_checkpoint(Path(path)))
            elif parsed.path == "/api/run-start":
                name = qs.get("name", ["run"])[0]
                steps = int(qs.get("steps", ["0"])[0])
                chunk = int(qs.get("chunk", ["100000"])[0])
                self._json(self.state.run_start(name, steps, chunk))
            elif parsed.path == "/api/stop":
                self._json(self.state.stop())
            elif parsed.path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, name="hwemu-http-shutdown", daemon=True).start()
            elif parsed.path == "/api/logs/clear":
                self._json(self.state.clear_logs())
            elif parsed.path == "/api/step":
                steps = int(qs.get("steps", ["250000"])[0])
                self._json(self.state.step(steps))
            elif parsed.path == "/api/key":
                down = qs.get("down", ["1"])[0] not in {"0", "false", "False"}
                self._json(self.state.key(int(qs.get("code", ["0"])[0]), down))
            elif parsed.path == "/api/touch":
                x = int(qs.get("x", ["0"])[0])
                y = int(qs.get("y", ["0"])[0])
                down = qs.get("down", ["1"])[0] not in {"0", "false", "False"}
                self._json(self.state.touch(x, y, down))
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
                return

    def log_message(self, fmt: str, *args: object) -> None:
        if not getattr(self.state.args, "quiet", False):
            super().log_message(fmt, *args)
