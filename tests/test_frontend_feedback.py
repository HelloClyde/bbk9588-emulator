from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from emu.web.frontend_server import build_feedback_archive


class _FeedbackState:
    def __init__(self, *, running: bool = True, frame: bytes | None = b"\x89PNG\r\n\x1a\nframe") -> None:
        self.running = running
        self.frame = frame
        self.dump_calls = 0

    def snapshot(self, *, detail: str) -> dict[str, object]:
        return {"running": self.running, "detail": detail, "pc": "0x80001234"}

    def logs(self, limit: int) -> dict[str, object]:
        return {
            "count": 2,
            "limit": limit,
            "events": [
                {"stream": "stdout", "text": "booted"},
                {"stream": "stderr", "text": "warning"},
            ],
        }

    def cached_frame(self) -> bytes | None:
        return self.frame

    def dump_frame(self) -> bytes:
        self.dump_calls += 1
        return b"\x89PNG\r\n\x1a\ndumped"


class FrontendFeedbackTests(unittest.TestCase):
    def test_feedback_archive_contains_runtime_evidence(self) -> None:
        state = _FeedbackState()
        captured_at = datetime(2026, 7, 16, 8, 9, 10, tzinfo=timezone.utc)

        name, payload = build_feedback_archive(
            state,
            client={"user_agent": "test-browser"},
            captured_at=captured_at,
        )

        self.assertEqual(name, "bbk9588-feedback-20260716-080910Z.zip")
        self.assertEqual(state.dump_calls, 1)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"manifest.json", "status.json", "logs.json", "qemu.log", "screen.png"},
            )
            manifest = json.loads(archive.read("manifest.json"))
            status = json.loads(archive.read("status.json"))
            self.assertEqual(manifest["client"]["user_agent"], "test-browser")
            self.assertEqual(status["detail"], "traces")
            self.assertIn("[stderr] warning", archive.read("qemu.log").decode("utf-8"))
            self.assertTrue(archive.read("screen.png").startswith(b"\x89PNG"))

    def test_feedback_archive_does_not_start_stopped_emulator_for_screenshot(self) -> None:
        state = _FeedbackState(running=False, frame=None)

        _name, payload = build_feedback_archive(state)

        self.assertEqual(state.dump_calls, 0)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertNotIn("screen.png", archive.namelist())

    def test_frontend_exposes_feedback_download(self) -> None:
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "emu/web/frontend.py").read_text(encoding="utf-8")
        server = (root / "emu/web/frontend_server.py").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertIn('id="saveFeedback"', frontend)
        self.assertIn("saveFeedbackEl.onclick = saveFeedbackBundle;", frontend)
        self.assertIn("fetch('/api/feedback'", frontend)
        self.assertIn('elif parsed.path == "/api/feedback":', server)
        self.assertIn("bbk9588-feedback-*.zip", readme)


if __name__ == "__main__":
    unittest.main()
