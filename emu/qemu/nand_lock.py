"""Cross-process ownership for an active BBK9588 NAND image."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO


class NandImageInUseError(RuntimeError):
    """Raised when another emulator process owns the selected NAND image."""


def _lock_key(path: Path) -> str:
    canonical = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lock_file(path: Path) -> Path:
    root = Path(tempfile.gettempdir()) / "bbk9588-emulator" / "nand-locks"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{_lock_key(path)}.lock"


def _try_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError from exc
        return

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise BlockingIOError from exc


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class NandImageLease:
    """Hold exclusive process ownership of one resolved NAND path."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._stream: BinaryIO | None = None

    def acquire(self, path: Path) -> Path:
        selected = path.resolve()
        if self.path is not None and os.path.normcase(str(self.path)) == os.path.normcase(str(selected)):
            return selected

        lock_path = _lock_file(selected)
        stream = lock_path.open("a+b")
        try:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            _try_lock(stream)
        except BlockingIOError:
            stream.close()
            raise NandImageInUseError(
                f"NAND image is already in use by another emulator: {selected}"
            ) from None
        except Exception:
            stream.close()
            raise

        previous = self._stream
        self._stream = stream
        self.path = selected
        if previous is not None:
            try:
                _unlock(previous)
            finally:
                previous.close()
        return selected

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        self.path = None
        if stream is None:
            return
        try:
            _unlock(stream)
        finally:
            stream.close()

