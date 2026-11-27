#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "cyclopts>=2.0",
#   "networkx>=3.0",
#   "markdown-it-py[linkify]",
#   "pydantic>=2.0",
# ]
# ///

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "itree"

from .cli import app

if __name__ == "__main__":
    app()
