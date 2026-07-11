"""Small WebSocket helpers for the BBK 9588 frontend."""

from __future__ import annotations

import base64
import hashlib

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def websocket_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_ws_frame(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    masked = mask is not None
    if mask is not None and len(mask) != 4:
        raise ValueError("WebSocket mask must be exactly 4 bytes")

    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.extend((mask_bit | 126, (length >> 8) & 0xFF, length & 0xFF))
    else:
        header.append(mask_bit | 127)
        header.extend(length.to_bytes(8, "big"))

    if not masked:
        return bytes(header) + payload
    masked_payload = bytes(value ^ mask[idx % 4] for idx, value in enumerate(payload))
    return bytes(header) + mask + masked_payload


class WebSocketFrameReader:
    """Timeout-safe WebSocket frame reader with a persistent partial-frame buffer."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.buffer = bytearray()

    def _recv_exact(self, size: int) -> bytes | None:
        while len(self.buffer) < size:
            chunk = self.connection.recv(size - len(self.buffer))
            if not chunk:
                return None
            self.buffer.extend(chunk)
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out

    def read_frame(self) -> tuple[int, bytes] | None:
        first = self._recv_exact(2)
        if first is None:
            return None
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            raw_length = self._recv_exact(2)
            if raw_length is None:
                return None
            length = int.from_bytes(raw_length, "big")
        elif length == 127:
            raw_length = self._recv_exact(8)
            if raw_length is None:
                return None
            length = int.from_bytes(raw_length, "big")

        mask = b""
        if masked:
            mask = self._recv_exact(4)
            if mask is None:
                return None

        payload = self._recv_exact(length)
        if payload is None:
            return None
        if masked:
            payload = bytes(value ^ mask[idx % 4] for idx, value in enumerate(payload))
        return opcode, payload

    def recv_text(self) -> str | None:
        frame = self.read_frame()
        if frame is None:
            return None
        opcode, payload = frame
        if opcode == 0x8:
            return None
        if opcode != 0x1:
            return ""
        return payload.decode("utf-8", errors="replace")


def read_ws_frame(connection) -> tuple[int, bytes] | None:
    return WebSocketFrameReader(connection).read_frame()


def recv_ws_text(connection) -> str | None:
    return WebSocketFrameReader(connection).recv_text()
