from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import pytest

from itree.models import FixtureState

SOURCE = Path(__file__).resolve().parents[1]
SETUP = SOURCE / "tests" / "fixtures" / "work-package" / "setup.py"
EntryPoint = Literal["installed", "module", "direct-cli", "direct-main"]


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
    evidence_prefix = parent.parent / parent.name
    journal_path = parent.parent.joinpath(f"{parent.name}-family-journal.jsonl")
    raw_path = parent.parent.joinpath(f"{parent.name}-raw-timings.json")
    final_path = parent.parent.joinpath(f"{parent.name}-teardown-timings.json")
    operation_started = time.perf_counter()
    setup: subprocess.CompletedProcess[str] | None = None
    setup_seconds: float | None = None
    case_seconds: float | None = None
    families_complete = False
    complete = False
    try:
        setup_started = time.perf_counter()
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
        setup_seconds = time.perf_counter() - setup_started
        evidence_prefix.with_name(f"{parent.name}-setup.stdout.log").write_text(
            setup.stdout, encoding="utf-8"
        )
        evidence_prefix.with_name(f"{parent.name}-setup.stderr.log").write_text(
            setup.stderr, encoding="utf-8"
        )
        assert setup.returncode == 0, (
            f"fixture setup failed\nstdout={setup.stdout}\nstderr={setup.stderr}"
        )
        case_started = time.perf_counter()
        yield FixtureState.model_validate_json(state_path.read_text(encoding="utf-8"))
        case_seconds = time.perf_counter() - case_started
        state = FixtureState.model_validate_json(state_path.read_text(encoding="utf-8"))
        families: dict[str, dict[str, float | int]] = {}
        journal_rows = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        for row in journal_rows:
            family = families.setdefault(
                row["family"],
                {
                    "requests": 0,
                    "objects": 0,
                    "api_seconds": 0.0,
                    "pacing_seconds": 0.0,
                },
            )
            family["requests"] += 1
            family["objects"] += row["object_count"]
            family["api_seconds"] += row["api_seconds"]
            family["pacing_seconds"] += row["pacing_seconds"]
        expected_objects = {
            "createIssue": 15,
            "createPullRequest": 1,
            "addComment": 119,
            "addSubIssue": 2,
        }
        actual_objects = {
            name: families.get(name, {}).get("objects") for name in expected_objects
        }
        assert actual_objects == expected_objects, (
            f"incomplete fixture families; cleanup is forbidden: {actual_objects}"
        )
        families_complete = True
        case_logs = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(root.joinpath("logs").iterdir())
            if path.is_file()
        }
        raw = {
            "complete_families": True,
            "setup_seconds": setup_seconds,
            "case_seconds": case_seconds,
            "setup_returncode": setup.returncode,
            "families": families,
            "journal": str(journal_path),
            "case_logs": case_logs,
        }
        raw_path.write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        retained_raw = json.loads(raw_path.read_text(encoding="utf-8"))

        deletion_started = time.perf_counter()
        deletion = subprocess.run(
            ["gh", "repo", "delete", state.repo, "--yes"],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        remote_delete_seconds = time.perf_counter() - deletion_started
        evidence_prefix.with_name(f"{parent.name}-remote-delete.json").write_text(
            json.dumps(
                {
                    "exit": deletion.returncode,
                    "stdout": deletion.stdout,
                    "stderr": deletion.stderr,
                    "elapsed_seconds": remote_delete_seconds,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        assert deletion.returncode == 0, (
            f"fixture deletion failed\nstdout={deletion.stdout}\nstderr={deletion.stderr}"
        )
        absence_started = time.perf_counter()
        absent = subprocess.run(
            ["gh", "repo", "view", state.repo],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        absence_seconds = time.perf_counter() - absence_started
        assert absent.returncode != 0, f"fixture remote remains: {state.repo}"
        scratch_started = time.perf_counter()
        trashed = subprocess.run(
            ["trash", str(parent)],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        scratch_seconds = time.perf_counter() - scratch_started
        assert trashed.returncode == 0, (
            f"fixture scratch cleanup failed\nstdout={trashed.stdout}\nstderr={trashed.stderr}"
        )
        finished = time.perf_counter()
        assert not parent.exists()
        timings = {
            **retained_raw,
            "remote_delete_seconds": remote_delete_seconds,
            "absence_verification_seconds": absence_seconds,
            "scratch_trash_seconds": scratch_seconds,
            "cleanup_seconds": finished - deletion_started,
            "total_seconds": finished - operation_started,
            "remote_delete_exit": deletion.returncode,
            "remote_absence_exit": absent.returncode,
            "scratch_trash_exit": trashed.returncode,
            "raw_record": str(raw_path),
        }
        durations = [
            timings["setup_seconds"],
            timings["case_seconds"],
            timings["cleanup_seconds"],
            timings["total_seconds"],
            timings["remote_delete_seconds"],
            timings["absence_verification_seconds"],
            timings["scratch_trash_seconds"],
        ]
        assert all(isinstance(value, (float, int)) and value > 0 for value in durations)
        final_path.write_text(
            json.dumps(timings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        complete = True
        assert timings["total_seconds"] < 300, (
            f"complete cold invocation exceeds 300 seconds: {timings['total_seconds']}; "
            f"evidence={final_path}"
        )
    finally:
        if not complete:
            failure = {
                "complete_families": families_complete,
                "setup_seconds": setup_seconds,
                "case_seconds": case_seconds,
                "measured_until_seconds": time.perf_counter() - operation_started,
                "setup_returncode": setup.returncode if setup is not None else None,
                "setup_stdout": setup.stdout if setup is not None else None,
                "setup_stderr": setup.stderr if setup is not None else None,
                "journal": str(journal_path) if journal_path.exists() else None,
            }
            evidence_prefix.with_name(
                f"{parent.name}-incomplete-timings.json"
            ).write_text(
                json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )


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
    trace_log: Path | None = None,
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
    worker_name = os.environ.get("PYTEST_XDIST_WORKER", "serial")
    if path_gh is not None:
        bin_directory = invocation_directory / "transport-bin"
        bin_directory.mkdir(exist_ok=True)
        executable = bin_directory / "gh"
        if not executable.exists():
            executable.symlink_to(path_gh)
        environment["PATH"] = f"{bin_directory}{os.pathsep}{environment['PATH']}"
    if trace_log is not None:
        trace_log.parent.mkdir(parents=True, exist_ok=True)
        trace_log.write_text("", encoding="utf-8")
        environment["ITREE_GH_TRACE_LOG"] = str(trace_log)
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
    log_directory.joinpath(f"{name}-{worker_name}.stdout.md").write_text(
        result.stdout, encoding="utf-8"
    )
    log_directory.joinpath(f"{name}-{worker_name}.stderr.txt").write_text(
        result.stderr, encoding="utf-8"
    )
    log_directory.joinpath(f"{name}-{worker_name}.json").write_text(
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


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
def test_all_comment_pages_preserve_every_body_and_identifier_in_order(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.pagination,
        "module",
        tmp_path / "pagination",
        "T005-pagination",
        path_gh=fixture_state.root / "live-page-edit-gh.py",
    )

    assert result.returncode == 0, result.stderr
    live = json.loads(
        subprocess.check_output(
            [
                "gh",
                "issue",
                "view",
                str(fixture_state.pagination),
                "--repo",
                fixture_state.repo,
                "--json",
                "body,labels",
            ],
            text=True,
        )
    )
    assert live["body"] == "LIVE-BODY-EDIT-AFTER-FIRST-COMMENT-PAGE\n"
    assert {label["name"] for label in live["labels"]} == {"live-comment-page"}
    assert "LEGACY-PAGINATION-BODY-EXACT\n" in result.stdout
    assert "LIVE-BODY-EDIT-AFTER-FIRST-COMMENT-PAGE" not in result.stdout
    malformed = invoke_work(
        fixture_state,
        fixture_state.pagination,
        "module",
        tmp_path / "pagination-malformed-page",
        "T005-pagination-malformed-page",
        path_gh=fixture_state.malformed_issue_gh,
    )
    assert malformed.returncode == 3
    assert "Work package read failure" in malformed.stderr
    assert "read selected issue, comments, and native parent" in malformed.stderr
    previous = -1
    for index, body in enumerate(fixture_state.pagination_comments):
        assert result.stdout.count(body) == 1, f"comment {index} is missing or repeated"
        offset = result.stdout.index(body)
        assert offset > previous, f"comment {index} is out of GitHub order"
        previous = offset
    rendered_ids = [
        int(identifier)
        for identifier in re.findall(
            r"^- id=(\d+) author=", result.stdout, re.MULTILINE
        )
    ]
    assert rendered_ids == fixture_state.pagination_ids


def test_managed_and_legacy_layout_indexing_preserves_body_bytes(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    def read_layout(job: tuple[int, str]) -> subprocess.CompletedProcess[str]:
        number, name = job
        return invoke_work(
            fixture_state, number, "module", tmp_path / name, f"T005-{name}"
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        managed, legacy, fenced = executor.map(
            read_layout,
            (
                (fixture_state.selected, "managed"),
                (fixture_state.legacy, "legacy"),
                (fixture_state.fenced, "fenced"),
            ),
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


def test_internal_invariant_failures_are_not_reclassified_as_transport_errors(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "invariant-failure",
        "T019-invariant-failure",
        path_gh=fixture_state.assertion_gh,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "AssertionError" in result.stderr
    assert "FIXTURE-GRAPHQL-INVARIANT" in result.stderr
    assert "Work package read failure" not in result.stderr
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
    entries: tuple[EntryPoint, ...] = (
        "installed",
        "module",
        "direct-cli",
        "direct-main",
    )

    def invoke(candidate: EntryPoint) -> subprocess.CompletedProcess[str]:
        return invoke_work(
            fixture_state,
            fixture_state.selected,
            candidate,
            tmp_path / f"parity-{candidate}",
            f"T010-parity-{candidate}",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = dict(zip(entries, executor.map(invoke, entries), strict=True))
    for candidate, result in outputs.items():
        assert result.returncode == 0, result.stderr

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


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
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


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
@pytest.mark.fresh
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


def _source_section(output: str, url: str, *, present: bool) -> str:
    heading = f"### {'Source' if present else 'Unresolved'} {url}\n"
    start = output.index(heading)
    end = output.find("\n### ", start + len(heading))
    return output[start:] if end == -1 else output[start:end]


def _set_citation_body(state: FixtureState, body: str) -> None:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(state.citation_edge),
            "--repo",
            state.repo,
            "--body",
            body,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_annotated_tags_resolve_to_commits_and_retain_all_identities(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    urls = fixture_state.source_urls
    shared = _source_section(output, urls["shared_annotated"], present=True)
    assert "refs/heads/shared/annotated-work-package-ref kind=branch" in shared
    assert "refs/tags/shared/annotated-work-package-ref kind=tag" in shared
    assert f"resolved_commit_sha={fixture_state.head}" in shared
    assert (
        _source_section(output, urls["ambiguous_annotated"], present=False).count(
            "reason=ambiguous_ref"
        )
        == 1
    )


def test_issuecomment_subanchors_do_not_fall_back_to_issue_body(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    urls = fixture_state.source_urls
    present = _source_section(
        selected_package.stdout, urls["cited_comment_subanchor"], present=True
    )
    assert "CITED-COMMENT-BODY-EXACT" in present
    assert "CITED-ISSUE-BODY-EXACT" not in present
    assert "sub_anchor=work-package-type-model" in present
    malformed = _source_section(
        selected_package.stdout, urls["malformed_comment_anchor"], present=False
    )
    assert "reason=invalid" in malformed


def test_encoding_none_large_text_uses_the_actual_git_blob(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    expected = fixture_state.checkout.joinpath("docs/large.md").read_text(
        encoding="utf-8"
    )
    source = _source_section(
        selected_package.stdout, fixture_state.source_urls["large_text"], present=True
    )
    assert fixture_state.large_text_size > 1_048_576
    assert source.count(expected) == 1
    assert f"resolved_commit_sha={fixture_state.head}" in source


def test_blob_directories_and_issue_pull_requests_are_unsupported(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    urls = fixture_state.source_urls
    assert (
        _source_section(
            selected_package.stdout, urls["blob_directory"], present=False
        ).count("reason=unsupported_kind")
        == 1
    )
    assert (
        _source_section(
            selected_package.stdout, urls["submodule"], present=False
        ).count("reason=unsupported_kind")
        == 1
    )
    assert (
        _source_section(
            selected_package.stdout, urls["pull_issue"], present=False
        ).count("reason=unsupported_kind")
        == 1
    )


def test_renderer_places_section_index_before_comments(
    selected_package: subprocess.CompletedProcess[str],
) -> None:
    output = selected_package.stdout
    positions = [
        output.index(marker)
        for marker in (
            "## Selected issue",
            "## Section index",
            "Comment index:",
            "## Governing ancestors",
            "## Governing documents",
            "## Present cited sources",
            "## Unresolved citations",
        )
    ]
    assert positions == sorted(positions)


def test_punctuation_and_section_indexes_reference_exact_body_ranges(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    canonical_markdown = _source_section(
        output, fixture_state.source_urls["markdown"], present=True
    )
    assert (
        f"canonical_url={fixture_state.source_urls['markdown']}" in canonical_markdown
    )
    body = fixture_state.selected_body
    for name in ("original-contract", "requirements", "plan", "tasks"):
        start_marker = f"<!-- itree:{name}:start -->"
        end_marker = f"<!-- itree:{name}:end -->"
        start = body.index(start_marker) + len(start_marker) + 1
        end = body.index(end_marker)
        line_start = body[:start].count("\n") + 1
        line_end = body[:end].count("\n") + 1
        byte_start = len(body[:start].encode("utf-8"))
        byte_end = len(body[:end].encode("utf-8"))
        assert (
            f"- {name}: line_start={line_start} line_end={line_end} byte_start={byte_start} byte_end={byte_end}"
            in output
        )


def test_links_inside_fetched_sources_are_not_recursed(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    nested = _source_section(
        output, fixture_state.source_urls["nested_source"], present=True
    )
    assert fixture_state.source_urls["nested_target"] in nested
    assert f"citation={fixture_state.source_urls['nested_target']}" not in output


@pytest.mark.parametrize(
    ("case", "source_key", "transport", "reason"),
    (
        ("runtime", "shared", "citation_runtime_gh", "inaccessible"),
        ("pagination", "shared", "citation_shape_gh", "invalid"),
        ("content", "large_text", "content_shape_gh", "invalid"),
        ("owner", "malformed_owner", "", "invalid"),
    ),
)
@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
def test_citation_transport_and_parse_failures_have_typed_outcomes(
    fixture_state: FixtureState,
    tmp_path: Path,
    case: str,
    source_key: str,
    transport: str,
    reason: str,
) -> None:
    if source_key == "malformed_owner":
        body = "https://github.com/bad%20owner/repo/blob/main/docs/primary.md\n"
        url = "https://github.com/bad%20owner/repo/blob/main/docs/primary.md"
        gh_wrapper = None
    else:
        url = fixture_state.source_urls[source_key]
        body = f"{url}\n"
        wrappers = {
            "citation_runtime_gh": fixture_state.citation_runtime_gh,
            "citation_shape_gh": fixture_state.citation_shape_gh,
            "content_shape_gh": fixture_state.content_shape_gh,
        }
        gh_wrapper = wrappers[transport]
    _set_citation_body(fixture_state, body)
    result = invoke_work(
        fixture_state,
        fixture_state.citation_edge,
        "module",
        tmp_path / f"citation-{case}",
        f"T019-citation-{case}",
        path_gh=gh_wrapper,
    )

    assert result.returncode == 0, result.stderr
    assert (
        _source_section(result.stdout, url, present=False).count(f"reason={reason}")
        == 1
    )


def test_all_entry_points_have_zero_remote_mutations_and_read_only_graphql(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    entries: tuple[EntryPoint, ...] = (
        "installed",
        "module",
        "direct-cli",
        "direct-main",
    )
    issue_before = _live_issue(fixture_state.repo, fixture_state.selected)
    comments_before = _live_comments(fixture_state.repo, fixture_state.selected)
    status_before = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=fixture_state.checkout, text=True
    )

    def invoke(entry: EntryPoint) -> subprocess.CompletedProcess[str]:
        return invoke_work(
            fixture_state,
            fixture_state.selected,
            entry,
            tmp_path / f"zero-mutation-{entry}",
            f"T021-zero-mutation-{entry}",
            path_gh=fixture_state.trace_gh,
            trace_log=tmp_path / f"mutation-trace-{entry}.jsonl",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = dict(zip(entries, executor.map(invoke, entries), strict=True))
    for entry in entries:
        trace = tmp_path / f"mutation-trace-{entry}.jsonl"
        result = outputs[entry]

        assert result.returncode == 0, result.stderr
        events = [
            json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
        ]
        issue_endpoints = [
            argument
            for event in events
            for argument in event["argv"]
            if argument == f"repos/{fixture_state.repo}/issues/{fixture_state.selected}"
        ]
        assert issue_endpoints == [], issue_endpoints
        family_numbers = [
            argument
            for event in events
            if any("WorkPackageIssueFamily" in argument for argument in event["argv"])
            for argument in event["argv"]
            if argument.startswith("number=")
        ]
        assert family_numbers.count(f"number={fixture_state.selected}") == 1
        assert family_numbers.count(f"number={fixture_state.roadmap}") == 1
        assert len(family_numbers) == 2, family_numbers
        methods = [method for event in events for method in event["http"]]
        assert any(method.startswith("> POST /graphql ") for method in methods), entry
        assert not any(
            method.startswith(("> PUT ", "> PATCH ", "> DELETE ")) for method in methods
        )
        assert not any(
            method.startswith("> POST /") and not method.startswith("> POST /graphql ")
            for method in methods
        )
        for event in events:
            for index, argument in enumerate(event["argv"]):
                if argument == "-X" and index + 1 < len(event["argv"]):
                    assert event["argv"][index + 1] not in {
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                    }
            if "graphql" in event["argv"]:
                queries = [
                    argument
                    for argument in event["argv"]
                    if argument.startswith("query=")
                ]
                assert "mutation" not in " ".join(queries).lower(), queries
    assert _live_issue(fixture_state.repo, fixture_state.selected) == issue_before
    assert _live_comments(fixture_state.repo, fixture_state.selected) == comments_before
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=fixture_state.checkout, text=True
        )
        == status_before
    )


def test_selected_and_ancestor_content_emits_once_with_all_citation_provenance(
    selected_package: subprocess.CompletedProcess[str], fixture_state: FixtureState
) -> None:
    output = selected_package.stdout
    for body in (
        fixture_state.selected_body,
        fixture_state.roadmap_body,
        "BINDING-PRIOR-DECISION-EXACT\n",
        "ROADMAP-COMMENT-DECISION-EXACT\n",
    ):
        assert output.count(body) == 1, body[:80]
    for key in ("selected_issue", "roadmap_issue", "binding_self", "roadmap_comment"):
        url = fixture_state.source_urls[key]
        assert f"### Source {url}" in output
        assert f"requested_url={url}" in output
        assert f"canonical_url={url}" in output
    assert "emission=deduplicated" in output


def test_malformed_selected_issue_json_exits_three(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "malformed-selected-json",
        "T023-malformed-selected-json",
        path_gh=fixture_state.malformed_issue_gh,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert "Work package read failure" in result.stderr
    assert "Work package complete" not in result.stderr

    malformed_page = invoke_work(
        fixture_state,
        fixture_state.pagination,
        "module",
        tmp_path / "malformed-page-shape",
        "T023-malformed-page-shape",
        path_gh=fixture_state.root / "malformed-page-shape-gh.py",
    )
    assert malformed_page.returncode == 3
    assert malformed_page.stdout == ""
    assert "Work package read failure" in malformed_page.stderr
    assert "invalid issue or parent" in malformed_page.stderr
    assert f"{fixture_state.repo}#{fixture_state.pagination}" in malformed_page.stderr
    assert "AssertionError" not in malformed_page.stderr


def test_governing_document_shape_failure_exits_three(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "malformed-governing-json",
        "T023-malformed-governing-json",
        path_gh=fixture_state.governing_shape_gh,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert "Work package read failure" in result.stderr
    assert "Work package complete" not in result.stderr


class _TruncatedHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"12345")
        self.wfile.flush()
        self.close_connection = True


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
def test_incomplete_external_body_is_inaccessible(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/truncated.txt"
    _set_citation_body(fixture_state, f"{url}\n")
    result = invoke_work(
        fixture_state,
        fixture_state.citation_edge,
        "module",
        tmp_path / "incomplete-read",
        "T024-incomplete-read",
    )
    server.shutdown()
    thread.join()
    server.server_close()

    assert result.returncode == 0, result.stderr
    assert "12345" not in _source_section(result.stdout, url, present=False)
    assert (
        _source_section(result.stdout, url, present=False).count("reason=inaccessible")
        == 1
    )


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
def test_empty_query_and_fragment_delimiters_remain_distinct(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    urls = tuple(
        fixture_state.source_urls[key]
        for key in (
            "cited_issue",
            "cited_issue_empty_query",
            "cited_issue_empty_fragment",
        )
    )
    _set_citation_body(fixture_state, "\n".join(f"<{url}>" for url in urls) + "\n")
    result = invoke_work(
        fixture_state,
        fixture_state.citation_edge,
        "module",
        tmp_path / "empty-delimiters",
        "T025-empty-delimiters",
    )

    assert result.returncode == 0, result.stderr
    for url in urls:
        assert (
            _source_section(result.stdout, url, present=True).count(
                f"canonical_url={url}"
            )
            == 1
        )
        assert f"requested_url={url}" in result.stdout
    assert urls[1].endswith("?")
    assert urls[2].endswith("#")


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
def test_non_commit_tag_targets_are_typed_unsupported(
    selected_package: subprocess.CompletedProcess[str],
    fixture_state: FixtureState,
    tmp_path: Path,
) -> None:
    for key in ("lightweight_tree", "annotated_tree"):
        unresolved = _source_section(
            selected_package.stdout, fixture_state.source_urls[key], present=False
        )
        assert unresolved.count("reason=unsupported_kind") == 1
        assert "AssertionError" not in selected_package.stderr
        assert "resolved_commit_sha" not in unresolved

    intent_sha = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{fixture_state.repo}/contents/INTENT.md?ref=main",
            "--jq",
            ".sha",
        ],
        text=True,
    ).strip()
    deleted = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "DELETE",
            f"repos/{fixture_state.repo}/contents/INTENT.md",
            "-f",
            "message=Verify absent governing document handling",
            "-f",
            f"sha={intent_sha}",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert deleted.returncode == 0, deleted.stderr
    absent = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "absent-governing-document",
        "T030-absent-governing-document",
    )
    assert absent.returncode == 0, absent.stderr
    assert "path=INTENT.md\nkind=absent reason=not_found" in absent.stdout
    assert "Fixture intent: INTENT-GOAL-TEXT." not in absent.stdout
    for content in (
        "Fixture agent rules: ORIGINAL-RULE-TEXT.",
        "Fixture constitution: CONSTITUTION-GOAL-TEXT.",
        "Fixture contribution rules: CONTRIBUTION-RULE-TEXT.",
        "Fixture workflow: ORGANIZATION-MODEL-TEXT.",
    ):
        assert content in absent.stdout


def test_malformed_governing_base64_exits_three(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    result = invoke_work(
        fixture_state,
        fixture_state.selected,
        "module",
        tmp_path / "malformed-governing-base64",
        "T028-malformed-governing-base64",
        path_gh=fixture_state.base64_error_gh,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert "Work package read failure" in result.stderr
    assert "Work package complete" not in result.stderr


@pytest.mark.xdist_group("mutable")
@pytest.mark.serial
def test_missing_full_sha_commit_is_not_found_and_noncommit_is_unsupported(
    fixture_state: FixtureState, tmp_path: Path
) -> None:
    url = fixture_state.source_urls["missing_commit"]
    _set_citation_body(
        fixture_state,
        f"{url}\n{fixture_state.source_urls['lightweight_tree']}\n{fixture_state.source_urls['annotated_tree']}\n",
    )
    result = invoke_work(
        fixture_state,
        fixture_state.citation_edge,
        "module",
        tmp_path / "missing-full-sha",
        "T029-missing-full-sha",
    )

    assert result.returncode == 0, result.stderr
    assert (
        _source_section(result.stdout, url, present=False).count("reason=not_found")
        == 1
    )
    for key in ("lightweight_tree", "annotated_tree"):
        section = _source_section(
            result.stdout, fixture_state.source_urls[key], present=False
        )
        assert section.count("reason=unsupported_kind") == 1
        assert "resolved_commit_sha" not in section
