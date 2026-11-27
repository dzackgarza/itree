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


def test_all_comment_pages_preserve_every_body_and_identifier_in_order(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.pagination,
        "module",
        tmp_path / "pagination",
        "T005-pagination",
    )

    assert result.returncode == 0, result.stderr
    previous = -1
    for index, body in enumerate(fixture_state.pagination_comments):
        assert result.stdout.count(body) == 1, f"comment {index} is missing or repeated"
        offset = result.stdout.index(body)
        assert offset > previous, f"comment {index} is out of GitHub order"
        previous = offset
    for identifier in (
        fixture_state.pagination_ids[0],
        fixture_state.pagination_ids[50],
        fixture_state.pagination_ids[-1],
    ):
        assert f"id={identifier}" in result.stdout


def test_managed_and_legacy_layout_indexing_preserves_body_bytes(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    managed = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "managed",
        "T005-managed",
    )
    legacy = invoke_work(
        fixture_state,
        fixture_state.legacy,
        "module",
        tmp_path / "legacy",
        "T005-legacy",
    )
    fenced = invoke_work(
        fixture_state,
        fixture_state.fenced,
        "module",
        tmp_path / "fenced",
        "T005-fenced",
    )

    for result, body in (
        (managed, fixture_state.selected_body),
        (legacy, fixture_state.legacy_body),
        (fenced, fixture_state.fenced_body),
    ):
        assert result.returncode == 0, result.stderr
        assert result.stdout.count(body) == 1
    assert "Section index" in managed.stdout
    assert "original-contract" in managed.stdout
    assert "requirements" in managed.stdout
    assert "plan" in managed.stdout
    assert "tasks" in managed.stdout
    assert "line_start=" in managed.stdout
    assert "byte_start=" in managed.stdout
    for result in (legacy, fenced):
        assert result.stdout.count("reason=legacy_zero_marker") == 3
        assert "kind=legacy" in result.stdout
        assert "kind=managed" not in result.stdout


@pytest.mark.parametrize(
    "kind", ("duplicate", "nested", "unknown", "partial", "reordered", "unterminated")
)
def test_invalid_layout_returns_readable_content_and_exit_two(
    fixture_state: FixtureState, tmp_path: Path, kind: str
) -> None:
    number = fixture_state.malformed[kind]
    result = invoke_work(
        fixture_state,
        number,
        "module",
        tmp_path / f"invalid-{kind}",
        f"T005-invalid-{kind}",
    )

    assert result.returncode == 2
    assert result.stdout.count(fixture_state.malformed_text[kind]) == 1
    assert f"MALFORMED-{kind.upper()}-COMMENT" in result.stdout
    assert kind in result.stderr
    assert f"{fixture_state.repo}#{number}" in result.stderr
    assert "Section index" not in result.stdout
    assert "Work package complete" not in result.stdout
    assert "Work package complete" not in result.stderr


def test_native_parent_cycle_returns_readable_ancestors_and_exit_two(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.cycle_a,
        "module",
        tmp_path / "cycle",
        "T005-parent-cycle",
        path_gh=fixture_state.cycle_gh,
    )

    assert result.returncode == 2
    assert "CYCLE-A-READABLE-BODY" in result.stdout
    assert "CYCLE-A-READABLE-COMMENT" in result.stdout
    assert "CYCLE-B-READABLE-BODY" in result.stdout
    assert "CYCLE-B-READABLE-COMMENT" in result.stdout
    assert "Section index" not in result.stdout
    assert (
        f"{fixture_state.repo}#{fixture_state.cycle_a} -> {fixture_state.repo}#{fixture_state.cycle_b} -> {fixture_state.repo}#{fixture_state.cycle_a}"
        in result.stderr
    )
    assert "Work package complete" not in result.stdout
    assert "Work package complete" not in result.stderr


def test_unreadable_selected_issue_exits_three_without_package(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        999_999_999,
        "module",
        tmp_path / "missing-issue",
        "T005-missing-issue",
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert f"{fixture_state.repo}#999999999" in result.stderr
    assert "Work package complete" not in result.stderr


def test_required_comment_read_failure_exits_three_without_package(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "failed-comments",
        "T005-failed-comments",
        path_gh=fixture_state.fail_comments_gh,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert "FIXTURE-REQUIRED-COMMENT-READ-FAILURE" in result.stderr
    assert "Work package complete" not in result.stderr


def test_all_public_entry_points_emit_the_same_live_package(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    for script in (
        SOURCE / "src" / "itree" / "cli.py",
        SOURCE / "src" / "itree" / "__main__.py",
    ):
        warmup = subprocess.run(
            ["uv", "run", "--script", str(script), "--help"],
            cwd=SOURCE,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        assert warmup.returncode == 0, (
            f"direct entry dependency setup failed\nstdout={warmup.stdout}\nstderr={warmup.stderr}"
        )
    outputs: dict[EntryPoint, subprocess.CompletedProcess[str]] = {}
    for candidate in ("installed", "module", "direct-cli", "direct-main"):
        outputs[candidate] = invoke_work(
            fixture_state,
            fixture_state.selected,
            candidate,
            tmp_path / f"parity-{candidate}",
            f"T010-parity-{candidate}",
        )
        assert outputs[candidate].returncode == 0, outputs[candidate].stderr

    baseline = outputs["module"]
    for candidate, result in outputs.items():
        assert result.stdout == baseline.stdout, (
            f"entry-point package differs: {candidate}"
        )
        assert result.stderr == baseline.stderr, (
            f"entry-point diagnostics differ: {candidate}"
        )
    assert fixture_state.selected_body in baseline.stdout
    assert "DECOY-FEATURE-POINTER" not in baseline.stdout
    assert "DECOY-CACHED-PLAN" not in baseline.stdout
    assert "DECOY-SPECIFICATION" not in baseline.stdout


@pytest.fixture(scope="session")
def selected_package(fixture_state: FixtureState) -> subprocess.CompletedProcess[str]:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        fixture_state.root / "selected-package-invocation",
        "T011-selected-package",
    )
    assert result.returncode == 0, result.stderr
    return result


def test_governing_documents_use_the_resolved_default_branch(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    for role, content in (
        ("AGENTS.md", "ORIGINAL-RULE-TEXT"),
        ("INTENT.md", "INTENT-GOAL-TEXT"),
        (".specify/memory/constitution.md", "CONSTITUTION-GOAL-TEXT"),
        ("CONTRIBUTING.md", "CONTRIBUTION-RULE-TEXT"),
        ("src/itree/WORKFLOWS.md", "ORGANIZATION-MODEL-TEXT"),
    ):
        assert role in output
        assert f"resolved_commit_sha={fixture_state.head}" in output
        assert content in output


def test_exact_source_content_and_complete_ref_provenance(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    urls = fixture_state.source_urls
    assert urls["pinned_blob"] in output
    assert f"requested_ref={fixture_state.pinned}" in output
    assert "Pinned source content: BEFORE-BRANCH-EDIT.\n" in output
    assert urls["main_blob"] in output
    assert f"requested_ref=main" in output
    assert "Changed source content: AFTER-BRANCH-EDIT.\n" in output
    for key in ("slash_branch", "slash_tag"):
        assert urls[key] in output
        assert f"resolved_commit_sha={fixture_state.head}" in output
    assert "refs/heads/shared/work-package-ref kind=branch" in output
    assert "refs/tags/shared/work-package-ref kind=tag" in output
    assert "CITED-ISSUE-BODY-EXACT" in output
    assert "CITED-COMMENT-BODY-EXACT" in output


def test_external_text_is_exact_and_alias_deduplication_preserves_identity(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    urls = fixture_state.source_urls
    assert output.count(fixture_state.external_text["markdown"]) == 1
    assert output.count(fixture_state.external_text["plain"]) == 3
    assert output.count(fixture_state.external_text["html"]) == 1
    assert urls["markdown_alias"] in output
    assert "aliases=" in output
    for key in ("plain_query", "plain_fragment", "plain_other_fragment"):
        assert f"canonical_url={urls[key]}" in output
        assert f"requested_url={urls[key]}" in output
        assert f"final_url={urls[key]}" in output
    assert "media_type=text/markdown" in output or "media_type=text/plain" in output
    assert "media_type=text/html" in output


def test_every_unsupported_or_failed_source_has_a_typed_outcome(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    urls = fixture_state.source_urls
    expected = {
        "inaccessible_external": "inaccessible",
        "redirect_failed": "redirect_failed",
        "invalid_external": "invalid_text_encoding",
        "binary_external": "binary",
        "media_external": "unsupported_media_type",
        "ambiguous": "ambiguous_ref",
        "unknown": "unknown_ref",
        "not_found": "not_found",
        "binary_blob": "binary",
        "invalid_blob": "invalid_text_encoding",
        "directory": "unsupported_kind",
        "pull": "unsupported_kind",
    }
    for key, reason in expected.items():
        assert urls[key] in output, key
        assert f"reason={reason}" in output, key
    assert f"#{fixture_state.pagination}" in output
    assert "reason=bare_reference" in output


def test_citation_resolution_is_limited_to_selected_issue_one_hop(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    assert "Pinned source content: BEFORE-BRANCH-EDIT.\n" in output
    assert "ANCESTOR-ONLY-CITATION-MUST-NOT-BE-FETCHED" not in output
    assert "LEGACY-PAGINATION-BODY-EXACT" not in output
    assert "ROADMAP-COMMENT-DECISION-EXACT" in output
    assert "DECOY-FEATURE-POINTER" not in output
    assert "DECOY-CACHED-PLAN" not in output
    assert "DECOY-SPECIFICATION" not in output


def _live_issue(repo: str, number: int) -> str:
    return subprocess.check_output(
        ["gh", "api", f"repos/{repo}/issues/{number}"], text=True
    )


def _live_comments(repo: str, number: int) -> str:
    return subprocess.check_output(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/comments?per_page=100",
        ],
        text=True,
    )


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_process_sees_new_live_comment_with_identical_original_body(
    fixture_state: FixtureState,
) -> None:
    original_before = _live_issue(fixture_state.repo, fixture_state.selected)
    first_directory = fixture_state.root / "fresh-process-first"
    second_directory = fixture_state.root / "fresh-process-second"
    first = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        first_directory,
        "T019-first-read",
    )
    assert first.returncode == 0, first.stderr
    live_body = f"LIVE-COMMENT-FROM-AFTER-THE-FIRST-READ-{time.time_ns()}\n"
    posted = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{fixture_state.repo}/issues/{fixture_state.selected}/comments",
            "--input",
            "-",
        ],
        input=json.dumps({"body": live_body}),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert posted.returncode == 0, posted.stderr
    second = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        second_directory,
        "T019-second-read",
    )
    original_after = _live_issue(fixture_state.repo, fixture_state.selected)

    assert second.returncode == 0, second.stderr
    assert live_body not in first.stdout
    assert second.stdout.count(live_body) == 1
    assert (
        json.loads(original_before)["body"]
        == json.loads(original_after)["body"]
        == fixture_state.selected_body
    )
    assert first.stdout.count(fixture_state.selected_body) == 1
    assert second.stdout.count(fixture_state.selected_body) == 1
    fixture_state.root.joinpath("T019-new-comment.json").write_text(
        posted.stdout, encoding="utf-8"
    )


def test_read_only_execution_preserves_local_and_github_state(
    fixture_state: FixtureState,
) -> None:
    invocation = fixture_state.root / "read-only-invocation"
    governing_paths = (
        "AGENTS.md",
        "INTENT.md",
        "CONTRIBUTING.md",
        ".specify/memory/constitution.md",
        "src/itree/WORKFLOWS.md",
    )
    instruction_before = {
        path: _file_hash(fixture_state.checkout / path) for path in governing_paths
    }
    issue_before = _live_issue(fixture_state.repo, fixture_state.pagination)
    comments_before = _live_comments(fixture_state.repo, fixture_state.pagination)
    status_before = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=fixture_state.checkout, text=True
    )
    result = invoke_work(
        fixture_state, fixture_state.pagination, "module", invocation, "T019-read-only"
    )

    assert result.returncode == 0, result.stderr
    instruction_after = {
        path: _file_hash(fixture_state.checkout / path) for path in governing_paths
    }
    status_after = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=fixture_state.checkout, text=True
    )

    assert instruction_before == instruction_after
    assert status_after == status_before
    assert _live_issue(fixture_state.repo, fixture_state.pagination) == issue_before
    assert (
        _live_comments(fixture_state.repo, fixture_state.pagination) == comments_before
    )
    assert (
        invocation.joinpath(".specify/feature.json").read_text(encoding="utf-8")
        == "DECOY-FEATURE-POINTER\n"
    )
    assert (
        invocation.joinpath(".itree/work/999/plan.md").read_text(encoding="utf-8")
        == "DECOY-CACHED-PLAN\n"
    )
    assert (
        invocation.joinpath("specs/999-wrong-issue/spec.md").read_text(encoding="utf-8")
        == "DECOY-SPECIFICATION\n"
    )
    assert "DECOY-FEATURE-POINTER" not in result.stdout
    assert "DECOY-CACHED-PLAN" not in result.stdout
    assert "DECOY-SPECIFICATION" not in result.stdout
