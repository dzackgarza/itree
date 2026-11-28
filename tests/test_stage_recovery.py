from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from itree.models import (
    ArtifactDraft,
    ArtifactProposal,
    IssueRef,
    RequestId,
    SessionReleaseRequest,
    StageModel,
)
from itree.stage import (
    artifact_upload_contents,
    build_upload_manifest,
    make_upload_parts,
)
from itree.stage import sha256_text
from itree.stage_writer import stage_comment_body
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


def test_unmanaged_body_change_after_session_entry_is_rejected(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-ghost")
    submitted = _write(fixture, "A-ghost", feature, writer=False)
    assert submitted.returncode == 5
    fixture.state["issue"]["body"] = fixture.issue_body().replace(
        "Plan zero α", "Unmanaged plan edit"
    )
    fixture.save()

    result = fixture.run("stage", "drain", ISSUE_REF, writer=True)

    assert result.returncode == 4
    assert "external_mutation" in result.stdout
    assert "Unmanaged plan edit" in fixture.issue_body()
    assert "Plan recovered β" not in fixture.issue_body()
    assert fixture.patches() == []


def test_edit_after_final_read_before_release_cannot_create_intent_or_patch(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-late-edit")
    process = fixture.spawn(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-late-edit",
        "--feature-directory",
        str(feature),
        writer=True,
        environment={"ITREE_STAGE_BARRIER_REQUEST": "request-A-late-edit"},
    )
    root = fixture.root / "writer"
    deadline = time.monotonic() + 30
    while not root.joinpath("checkpoint.json").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert root.joinpath("checkpoint.json").exists()
    fixture.load()
    proposal = next(
        comment
        for comment in fixture.state["comments"]
        if '"request_kind":"sections"' in comment["body"]
    )
    proposal["body"] += "late-edit"
    proposal["updated_at"] = "2026-11-28T02:00:00Z"
    fixture.state["histories"][proposal["node_id"]].update(
        {
            "updatedAt": proposal["updated_at"],
            "lastEditedAt": proposal["updated_at"],
            "editor": {"login": "other"},
            "userContentEdits": {"totalCount": 1, "nodes": [{"id": "edit"}]},
        }
    )
    fixture.save()
    root.joinpath("release.json").write_text("release")

    stdout, stderr = process.communicate(timeout=60)

    assert process.returncode == 4, stdout + stderr
    assert "edited_record" in stdout
    assert fixture.patches() == []
    assert not any(
        '"status":"intended"' in comment["body"]
        for comment in fixture.state["comments"]
    )


def test_edited_confirmed_receipt_is_invalidated_without_replay(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-receipt-edit")
    first = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-receipt-edit",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert first.returncode == 0
    fixture.load()
    receipt = next(
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-receipt")
        and '"status":"confirmed"' in comment["body"]
    )
    history = fixture.state["histories"][receipt["node_id"]]
    receipt["updated_at"] = "2026-11-28T02:00:00Z"
    history.update(
        {
            "updatedAt": receipt["updated_at"],
            "lastEditedAt": receipt["updated_at"],
            "editor": {"login": "other"},
            "userContentEdits": {"totalCount": 1, "nodes": [{"id": "late"}]},
        }
    )
    fixture.save()

    result = fixture.run("stage", "drain", ISSUE_REF, writer=True)

    assert result.returncode == 4
    assert "seal_invalidated" in result.stdout
    assert "replay" not in result.stdout
    assert fixture.patches() == []


def _post_model(fixture: StageFixture, fence: str, model: StageModel) -> None:
    result = subprocess.run(
        [
            str(fixture.bindir / "gh"),
            "api",
            "-X",
            "POST",
            f"repos/{SLUG}/issues/{ISSUE_NUMBER}/comments",
            "--input",
            "-",
        ],
        input=json.dumps(
            {"body": stage_comment_body(fence, model.model_dump(mode="json"))}
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=fixture._environment(False, None),
    )
    assert result.returncode == 0, result.stderr


def _release(
    fixture: StageFixture, attempt: str, request_id: str, expected_id: int
) -> None:
    record = SessionReleaseRequest(
        version=1,
        request_kind="session_release",
        issue_node_id="I_stage_node_71",
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id=request_id,
        attempt_id=attempt,
        session_id="session-" + attempt,
        expected_last_scanned_comment_id=expected_id,
    )
    _post_model(fixture, "itree-stage-edit", record)


def test_quiescent_confirmed_queue_releases_once(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-release")
    first = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-release",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert first.returncode == 0
    fixture.load()
    expected_id = max(comment["id"] for comment in fixture.state["comments"]) + 1
    _release(fixture, "A-release", "release-A-release", expected_id)

    result = fixture.run("stage", "drain", ISSUE_REF, writer=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "session_released=session-A-release" in result.stdout
    replay = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert replay.stdout.count("session_released=session-A-release:replay") == 1


def test_pending_upload_blocks_session_release_and_any_body_effect(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-block-release")
    first = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-block-release",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert first.returncode == 0
    proposal = ArtifactProposal(
        version=1,
        request_kind="artifacts",
        issue_node_id="I_stage_node_71",
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="incomplete-artifact",
        attempt_id="A-block-release",
        session_id="session-A-block-release",
        base_body_sha256=sha256_text(fixture.issue_body()),
        base_body=fixture.issue_body(),
        artifacts=(
            ArtifactDraft(key="incomplete", kind="evidence", content="still absent\n"),
        ),
    )
    manifest = build_upload_manifest(proposal, artifact_upload_contents(proposal))
    _post_model(fixture, "itree-stage-upload-manifest", manifest)
    fixture.load()
    expected_id = max(comment["id"] for comment in fixture.state["comments"]) + 1
    _release(fixture, "A-block-release", "release-A-block-release", expected_id)

    result = fixture.run("stage", "drain", ISSUE_REF, writer=True)

    assert result.returncode == 5
    assert "pending_upload" in result.stdout
    assert "release_blocked" in result.stdout
    assert "session_released" not in result.stdout
    assert fixture.patches() == []


def test_same_second_tampering_is_rejected_from_public_edit_history(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature = _prepare_changed_plan(fixture, "A-tamper")
    submitted = _write(fixture, "A-tamper", feature, writer=False)
    assert submitted.returncode == 5
    fixture.load()
    proposal = next(
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-edit")
        and '"request_kind":"sections"' in comment["body"]
    )
    proposal["body"] = proposal["body"].replace(
        "Plan recovered β", "Plan same-second tampered β"
    )
    proposal["updated_at"] = proposal["created_at"]
    fixture.state["histories"][proposal["node_id"]]["userContentEdits"][
        "totalCount"
    ] = 1
    fixture.save()

    result = fixture.run("stage", "drain", ISSUE_REF, writer=True)

    assert result.returncode == 4
    assert "edited_record" in result.stdout
    assert not any(
        '"status":"intended"' in comment["body"]
        for comment in fixture.state["comments"]
    )
    assert fixture.patches() == []
    assert "Plan same-second tampered β" not in fixture.issue_body()


def test_direct_proposal_cannot_bypass_older_pending_manifest(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("session-order")
    admitted = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "session-order",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    body = fixture.issue_body()
    manifest_proposal = ArtifactProposal(
        version=1,
        request_kind="artifacts",
        issue_node_id="I_stage_node_71",
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="older-manifest",
        attempt_id="session-order",
        session_id="session-session-order",
        base_body_sha256=sha256_text(body),
        base_body=body,
        artifacts=(
            ArtifactDraft(
                key="older-research", kind="research", content="Older research\n"
            ),
            ArtifactDraft(
                key="older-evidence", kind="evidence", content="Older evidence\n"
            ),
        ),
    )
    manifest = build_upload_manifest(
        manifest_proposal, artifact_upload_contents(manifest_proposal)
    )
    parts = make_upload_parts(manifest, artifact_upload_contents(manifest_proposal))
    _post_model(fixture, "itree-stage-upload-manifest", manifest)
    _post_model(fixture, "itree-stage-upload-part", parts[0])
    later_feature, _ = fixture.materialize("A-order")
    later_feature.joinpath("plan.md").write_text(
        'Later direct plan\n```itree-contract\nkind = "audit"\nevidence = "narrows"\n```\n'
    )

    blocked = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-order",
        "--feature-directory",
        str(later_feature),
        writer=True,
    )
    assert blocked.returncode == 5
    assert "pending_upload" in blocked.stdout
    assert fixture.patches() == []

    _post_model(fixture, "itree-stage-upload-part", parts[1])
    _post_model(fixture, "itree-stage-upload-part", parts[2])
    recovered = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert len(fixture.patches()) == 2
    assert "older-research" in fixture.issue_body()
    assert "older-evidence" in fixture.issue_body()
    assert "Later direct plan" in fixture.issue_body()
    replay = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert replay.returncode == 0
    assert len(fixture.patches()) == 2
