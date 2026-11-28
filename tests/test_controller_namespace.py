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
            tmp_path / "probe-does-not-read-fixture-state.json"
        ),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/controller_probe/test_shared_execution.py",
            "-n",
            "2",
            "--dist=loadgroup",
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
    collections = [
        json.loads(controller.joinpath("collection", f"{worker}.json").read_text())
        for worker in ("gw0", "gw1")
    ]
    readiness = [
        json.loads(controller.joinpath("worker-ready", f"{worker}.json").read_text())
        for worker in ("gw0", "gw1")
    ]
    assert {record["expected_cases"] for record in collections} == {2}
    assert {record["expected_readonly"] for record in collections} == {2}
    assert {record["expected_serial"] for record in collections} == {0}
    assert {record["root"] for record in collections} == {str(controller)}
    assert {record["root"] for record in readiness} == {str(controller)}
    assert len({record["pid"] for record in readiness}) == 2
    assert controller.joinpath("workers-ready").is_file()
    assert not controller.joinpath("worker-start-timeout").exists()
