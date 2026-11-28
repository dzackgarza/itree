from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_real_xdist_workers_share_the_explicit_controller_namespace(
    tmp_path: Path,
) -> None:
    controller = tmp_path / "one-controller"
    environment = {
        **os.environ,
        "ITREE_WORK_CONTROLLER_ROOT": str(controller),
        "ITREE_WORK_FIXTURE_STATE": str(
            tmp_path / "collect-only-does-not-read-fixture-state.json"
        ),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_work.py",
            "-n",
            "2",
            "--dist=loadgroup",
            "--collect-only",
            "-q",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(controller.joinpath("collection", f"{worker}.json").read_text())
        for worker in ("gw0", "gw1")
    ]
    assert {record["expected_cases"] for record in records} == {41}
    assert {record["expected_readonly"] for record in records} == {30}
    assert {record["expected_serial"] for record in records} == {11}
    assert {record["root"] for record in records} == {str(controller)}
    assert len({record["pid"] for record in records}) == 2
