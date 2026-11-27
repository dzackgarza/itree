from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

SOURCE = Path(__file__).resolve().parents[1]
SETUP = SOURCE / "tests" / "fixtures" / "work-package" / "setup.py"
EntryPoint = Literal["installed", "module", "direct-cli", "direct-main"]


class FixtureState(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    root: Path
    checkout: Path
    selected: int
    selected_body: str
    roadmap: int
    roadmap_body: str
    roadmap_comment_id: int
    roadmap_comment_url: str
    binding_comment_id: int
    binding_comment_url: str
    cited: int
    cited_comment_id: int
    pagination: int
    pagination_comments: list[str]
    pagination_ids: list[int]
    malformed: dict[str, int]
    malformed_text: dict[str, str]
    fenced: int
    fenced_body: str
    legacy: int
    legacy_body: str
    cycle_a: int
    cycle_b: int
    cycle_gh: Path
    fail_comments_gh: Path
    pinned: str
    head: str
    source_urls: dict[str, str]
    external_text: dict[str, str]
    setup_elapsed_seconds: float
    python: str


def _load_or_create_fixture() -> Iterator[FixtureState]:
    configured = os.environ.get("ITREE_WORK_FIXTURE_STATE")
    if configured is not None:
        yield FixtureState.model_validate_json(
            Path(configured).read_text(encoding="utf-8")
        )
        return
    parent = Path(tempfile.mkdtemp(prefix="itree-work-test-", dir="/tmp/opencode"))
    root = parent / "acceptance"
    state_path = parent / "state.json"
    setup = subprocess.run(
        [
            sys.executable,
            str(SETUP),
            "create",
            "--root",
            str(root),
            "--output",
            str(state_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=1200,
    )
    assert setup.returncode == 0, (
        f"fixture setup failed\nstdout={setup.stdout}\nstderr={setup.stderr}"
    )
    yield FixtureState.model_validate_json(state_path.read_text(encoding="utf-8"))
    state = FixtureState.model_validate_json(state_path.read_text(encoding="utf-8"))
    deletion = subprocess.run(
        ["gh", "repo", "delete", state.repo, "--yes"],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert deletion.returncode == 0, (
        f"fixture deletion failed\nstdout={deletion.stdout}\nstderr={deletion.stderr}"
    )
    absent = subprocess.run(
        ["gh", "repo", "view", state.repo],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert absent.returncode != 0, f"fixture remote remains: {state.repo}"
    trashed = subprocess.run(
        ["trash", str(parent)], text=True, capture_output=True, check=False, timeout=120
    )
    assert trashed.returncode == 0, (
        f"fixture scratch cleanup failed\nstdout={trashed.stdout}\nstderr={trashed.stderr}"
    )
    assert not parent.exists()


@pytest.fixture(scope="session")
def fixture_state() -> Iterator[FixtureState]:
    yield from _load_or_create_fixture()


def _arguments(state: FixtureState, number: int, entry: EntryPoint) -> list[str]:
    issue_ref = f"{state.repo}#{number}"
    if entry == "module":
        return [sys.executable, "-m", "itree", "work", issue_ref]
    if entry == "installed":
        return [str(SOURCE / ".venv" / "bin" / "itree"), "work", issue_ref]
    if entry == "direct-cli":
        return [
            "uv",
            "run",
            "--script",
            str(SOURCE / "src" / "itree" / "cli.py"),
            "work",
            issue_ref,
        ]
    return [
        "uv",
        "run",
        "--script",
        str(SOURCE / "src" / "itree" / "__main__.py"),
        "work",
        issue_ref,
    ]


def invoke_work(
    state: FixtureState,
    number: int,
    entry: EntryPoint,
    invocation_directory: Path,
    name: str,
    path_gh: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    invocation_directory.mkdir(parents=True, exist_ok=True)
    invocation_directory.joinpath(".specify").mkdir(exist_ok=True)
    invocation_directory.joinpath(".itree/work/999").mkdir(parents=True, exist_ok=True)
    invocation_directory.joinpath("specs/999-wrong-issue").mkdir(
        parents=True, exist_ok=True
    )
    invocation_directory.joinpath(".specify/feature.json").write_text(
        "DECOY-FEATURE-POINTER\n", encoding="utf-8"
    )
    invocation_directory.joinpath(".itree/work/999/plan.md").write_text(
        "DECOY-CACHED-PLAN\n", encoding="utf-8"
    )
    invocation_directory.joinpath("specs/999-wrong-issue/spec.md").write_text(
        "DECOY-SPECIFICATION\n", encoding="utf-8"
    )
    environment = os.environ.copy()
    if path_gh is not None:
        bin_directory = invocation_directory / "transport-bin"
        bin_directory.mkdir(exist_ok=True)
        executable = bin_directory / "gh"
        if not executable.exists():
            executable.symlink_to(path_gh)
        environment["PATH"] = f"{bin_directory}{os.pathsep}{environment['PATH']}"
    command = _arguments(state, number, entry)
    cwd = SOURCE if entry in ("direct-cli", "direct-main") else invocation_directory
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    elapsed = time.perf_counter() - started
    log_directory = state.root / "logs"
    log_directory.mkdir(exist_ok=True)
    log_directory.joinpath(f"{name}.stdout.md").write_text(
        result.stdout, encoding="utf-8"
    )
    log_directory.joinpath(f"{name}.stderr.txt").write_text(
        result.stderr, encoding="utf-8"
    )
    log_directory.joinpath(f"{name}.json").write_text(
        json.dumps(
            {
                "command": command,
                "cwd": str(cwd),
                "exit": result.returncode,
                "elapsed_seconds": elapsed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return result


def test_public_command_recovers_the_selected_live_contract(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "initial-red",
        "T003-selected-module",
    )

    assert result.returncode == 0, (
        f"missing public work command\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert fixture_state.selected_body in result.stdout
    assert "ORIGINAL-CONTRACT-EXACT" in result.stdout
    assert "REQUIREMENTS-EXACT" in result.stdout
    assert "PLAN-EXACT" in result.stdout
    assert "TASKS-EXACT" in result.stdout
    assert "BINDING-PRIOR-DECISION-EXACT" in result.stdout
    assert "CORRECTIVE-DECISION-EXACT" in result.stdout
    assert "OPEN-VERIFICATION-FINDING-EXACT" in result.stdout
    assert fixture_state.roadmap_body in result.stdout
    assert "ROADMAP-COMMENT-DECISION-EXACT" in result.stdout
