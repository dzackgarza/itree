from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import TypeAdapter


@pytest.mark.parametrize("slot", [0, 1])
def test_worker_starts_after_shared_readiness(slot: int) -> None:
    root = Path(os.environ["ITREE_WORK_CONTROLLER_ROOT"])
    owner = TypeAdapter(dict[str, int | str]).validate_json(
        root.joinpath("owner.json").read_text()
    )
    worker = os.environ["PYTEST_XDIST_WORKER"]
    assert int(owner["pid"]) > 0
    assert root.joinpath("collection", f"{worker}.json").is_file()
