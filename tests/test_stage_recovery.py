from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.stage_support import ISSUE_REF, ISSUE_NUMBER, SLUG, StageFixture


def _prepare_changed_plan(fixture: StageFixture, attempt: str) -> Path:
    feature, _ = fixture.materialize(attempt)
    feature.joinpath("plan.md").write_text(
        "Plan recovered β\n"
        "```itree-contract\n"
        'kind = "audit"\n'
        'evidence = "narrows"\n'
        "```\n",
        encoding="utf-8",
    )
    return feature


def _write(
    fixture: StageFixture, attempt: str, feature: Path, *, writer: bool = True
) -> subprocess.CompletedProcess[str]:
    return fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        attempt,
        "--feature-directory",
        str(feature),
        writer=writer,
    )


def test_http_rejection_retains_bytes_and_blocks_later_writes(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-http-rejected")
    fixture.state["patch_outcome"] = "http_reject"
    fixture.save()

    result = _write(fixture, "A-http-rejected", feature)

    assert result.returncode == 4
    assert "stage_complete=false" in result.stdout
    assert "github_http_rejection" in result.stdout
    assert "Plan recovered" not in fixture.issue_body()
    assert len(fixture.patches()) == 1
    assert "pending_suffix=" in result.stdout


def test_timeout_after_application_is_indeterminate_then_recovers_once(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-timeout")
    fixture.state["patch_outcome"] = "timeout"
    fixture.save()

    failed = _write(fixture, "A-timeout", feature)

    assert failed.returncode == 5
    assert "stage_complete=false" in failed.stdout
    assert "indeterminate" in failed.stdout
    assert feature.exists()
    assert len(fixture.patches()) == 1
    assert "Plan recovered β" in fixture.issue_body()

    fixture.state["patch_outcome"] = "success"
    fixture.save()
    resumed = fixture.run(
        "stage", "resume", ISSUE_REF, "--attempt", "A-timeout", writer=True
    )

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "recovered" in resumed.stdout
    assert len(fixture.patches()) == 1
    assert fixture.issue_body().count("Plan recovered β") == 1


@pytest.mark.parametrize("outcome", ["nonzero", "html", "malformed_success"])
def test_unusable_transport_results_stay_indeterminate(
    tmp_path: Path, outcome: str
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-" + outcome)
    fixture.state["patch_outcome"] = outcome
    fixture.save()

    result = _write(fixture, "A-" + outcome, feature)

    assert result.returncode == 5
    assert "stage_complete=false" in result.stdout
    assert "indeterminate" in result.stdout
    assert feature.exists()
    assert len(fixture.patches()) == 1


def test_repeated_request_without_new_bytes_has_no_second_effect(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-replay")
    replay = tmp_path / "identical-scratch"
    shutil.copytree(feature, replay)
    assert _write(fixture, "A-replay", feature).returncode == 0
    assert len(fixture.patches()) == 1

    result = _write(fixture, "A-replay", replay)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "replay" in result.stdout
    assert len(fixture.patches()) == 1


def test_changed_bytes_under_a_confirmed_request_id_conflict_without_an_effect(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    first = _prepare_changed_plan(fixture, "A-changed-id")
    assert _write(fixture, "A-changed-id", first).returncode == 0
    patches = len(fixture.patches())

    changed = _prepare_changed_plan(fixture, "A-changed-id")
    changed.joinpath("plan.md").write_text(
        "Plan different bytes β\n"
        "```itree-contract\n"
        'kind = "audit"\n'
        'evidence = "narrows"\n'
        "```\n",
        encoding="utf-8",
    )
    result = _write(fixture, "A-changed-id", changed)

    assert result.returncode == 4
    assert "duplicate_payload_conflict" in result.stdout
    assert len(fixture.patches()) == patches
    assert "Plan different bytes" not in fixture.issue_body()


@pytest.mark.parametrize(
    ("actor", "role"),
    [
        ("triager", "triage"),
        ("writer", "write"),
        ("maintainer", "maintain"),
        ("maintainer", "admin"),
    ],
)
def test_enrolled_standard_issue_roles_can_write(
    tmp_path: Path, actor: str, role: str
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    attempt = "A-role-" + role
    feature = _prepare_changed_plan(fixture, attempt)
    submitted = _write(fixture, attempt, feature, writer=False)
    assert submitted.returncode == 5
    fixture.load()
    section_proposals = [
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-edit")
        and '"request_kind":"sections"' in comment["body"]
    ]
    proposal = section_proposals[0]
    proposal_body = proposal["body"]
    session_comment = next(
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-edit")
        and '"request_kind":"session_admit"' in comment["body"]
    )
    session_body_lines = session_comment["body"].splitlines()
    session_payload = json.loads(session_body_lines[1])
    session_payload["enrolled_actors"] = ["maintainer", actor]
    session_body = (
        session_body_lines[0]
        + "\n"
        + json.dumps(session_payload, sort_keys=True, separators=(",", ":"))
        + "\n"
        + session_body_lines[2]
        + "\n"
    )
    fixture.state["comments"] = []
    fixture.save()
    enrolled = subprocess.run(
        [
            str(fixture.bindir / "gh"),
            "api",
            "-X",
            "POST",
            f"repos/{SLUG}/issues/{ISSUE_NUMBER}/comments",
            "--input",
            "-",
        ],
        input=json.dumps({"body": session_body}),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=fixture._environment(False, None),
    )
    assert enrolled.returncode == 0, enrolled.stderr
    fixture.load()
    fixture.state["actor"] = actor
    fixture.state["permissions"][actor] = role
    fixture.save()
    post = subprocess.run(
        [
            str(fixture.bindir / "gh"),
            "api",
            "-X",
            "POST",
            f"repos/{SLUG}/issues/{ISSUE_NUMBER}/comments",
            "--input",
            "-",
        ],
        input=json.dumps({"body": proposal_body}),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=fixture._environment(False, None),
    )
    assert post.returncode == 0, post.stderr

    result = _write(fixture, attempt, feature)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Plan recovered β" in fixture.issue_body()


def test_edited_durable_submission_is_rejected_before_patch(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-edited")
    submitted = _write(fixture, "A-edited", feature, writer=False)
    assert submitted.returncode == 5
    fixture.load()
    proposal = next(
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-edit")
        and '"request_kind":"sections"' in comment["body"]
    )
    proposal["body"] = proposal["body"] + "tampered"
    proposal["updated_at"] = "2026-11-28T01:00:00Z"
    history = fixture.state["histories"][proposal["node_id"]]
    history.update(
        {
            "updatedAt": proposal["updated_at"],
            "lastEditedAt": proposal["updated_at"],
            "editor": {"login": "other"},
            "userContentEdits": {"totalCount": 1, "nodes": [{"id": "edit-one"}]},
        }
    )
    fixture.save()

    result = fixture.run("stage", "drain", ISSUE_REF, writer=True)

    assert result.returncode == 4
    assert "edited_record" in result.stdout
    assert fixture.patches() == []
    assert "tampered" not in fixture.issue_body()
