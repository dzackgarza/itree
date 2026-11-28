from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_stage_materialize_is_a_public_issue_selected_command(tmp_path: Path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    executable.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "itree",
            "stage",
            "materialize",
            "owner/repo#71",
            "--attempt",
            "A-public-boundary",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )

    assert result.returncode == 0
    assert "issue_ref=owner/repo#71" in result.stdout
    assert "SPECIFY_FEATURE_NO_PERSIST=1" in result.stdout
