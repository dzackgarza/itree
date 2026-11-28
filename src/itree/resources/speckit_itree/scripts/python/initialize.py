#!/usr/bin/env python3
from __future__ import annotations

import os

from work_loader import launcher_path


def main() -> None:
    launcher = launcher_path()
    os.execv(launcher, [str(launcher), "harness-work", "initialize"])


if __name__ == "__main__":
    main()
