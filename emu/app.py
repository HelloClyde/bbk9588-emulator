#!/usr/bin/env python3
"""Application entry point for the BBK 9588 hardware emulator frontend."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emu.web.frontend import main as frontend_main


def main(argv: list[str] | None = None) -> int:
    """Start the local web frontend and its emulator backend worker."""
    return frontend_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
