#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cyclopts>=2.0",
#   "pydantic>=2.0",
# ]
# ///
from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    app()
