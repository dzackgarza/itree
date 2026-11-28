from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from itree.models import (
    ActorLogin,
    CommentHistory,
    CommentNodeId,
    ExternalSubmissionDetail,
    IssueComment,
    IssueNodeId,
    IssueRef,
    ReceiptEnvelope,
    ReceiptPayload,
    Sha256,
    SubmissionSeal,
    WriterSeal,
    WriterSealInvalidContext,
)
from itree.stage_writer import (
    WriterAuthorityEvidence,
    canonical_payload_digest,
    stage_comment_body,
    validate_submission_seal,
    validate_writer_seal,
)
from tests.stage_support import COMMIT_SHA, ISSUE_NODE_ID, ISSUE_REF, SLUG


def _native(body: str = "```itree-stage-edit\n{}\n```\n") -> IssueComment:
    return IssueComment(
        id=101,
        node_id=CommentNodeId("IC_proposal"),
        body=body,
        user={"login": "maintainer"},
        created_at="2026-11-28T00:00:00Z",
        updated_at="2026-11-28T00:00:00Z",
        html_url=f"https://github.com/{SLUG}/issues/71#issuecomment-101",
    )


def _history(
    *,
    updated_at: str = "2026-11-28T00:00:00Z",
    last_edited_at: str | None = None,
) -> CommentHistory:
    return CommentHistory(
        created_at="2026-11-28T00:00:00Z",
        updated_at=updated_at,
        last_edited_at=last_edited_at,
    )


def _writer_seal() -> WriterSeal:
    workflow = TypeAdapter(Sha256).validate_python("a" * 64)
    return WriterSeal(
        repository=SLUG,
        default_branch="main",
        default_head_sha=COMMIT_SHA,
        workflow_path=".github/workflows/itree-issue-writer.yml",
        workflow_blob="blob-workflow-v1",
        workflow_sha256=workflow,
        run_id=101,
        run_attempt=1,
        job_id=201,
        job_node_id="J_writer",
        job_name="write",
        run_url=f"https://github.com/{SLUG}/actions/runs/101",
        job_url=f"https://github.com/{SLUG}/actions/runs/101/job/201",
        actions_installation_identity="github-actions",
        effective_token_permissions={
            "contents": "read",
            "issues": "write",
            "actions": "read",
        },
    )


def _authority() -> WriterAuthorityEvidence:
    seal = _writer_seal()
    return WriterAuthorityEvidence(
        repository=seal.repository,
        default_branch=seal.default_branch,
        default_head_sha=seal.default_head_sha,
        observed_ref="main",
        workflow_path=seal.workflow_path,
        workflow_blob=seal.workflow_blob,
        workflow_sha256=seal.workflow_sha256,
        workflow_permissions={
            "contents": "read",
            "issues": "write",
            "actions": "read",
        },
        run_id=seal.run_id,
        run_attempt=seal.run_attempt,
        job_id=seal.job_id,
        job_node_id=seal.job_node_id,
        job_name=seal.job_name,
        run_url=seal.run_url,
        job_url=seal.job_url,
        actions_installation_identity=seal.actions_installation_identity,
        operation_log_matches=True,
        claimed_human_role=None,
    )


def test_submission_seal_checks_the_current_wire_and_complete_zero_history() -> None:
    payload = {"request_id": "A", "version": 1}
    native = _native(stage_comment_body("itree-stage-edit", payload))
    logical_digest = canonical_payload_digest(payload)

    valid = validate_submission_seal(
        native,
        _history(),
        IssueNodeId(ISSUE_NODE_ID),
        logical_digest,
        payload,
    )

    assert isinstance(valid, SubmissionSeal)
    assert valid.logical_payload_sha256 == logical_digest
    assert valid.history.last_edited_at is None


@pytest.mark.parametrize(
    "history",
    [
        _history(
            updated_at="2026-11-28T00:01:00Z",
            last_edited_at="2026-11-28T00:01:00Z",
        ),
        _history(last_edited_at="2026-11-28T00:01:00Z"),
    ],
)
def test_edited_metadata_cannot_be_authorized_by_the_same_author(
    history: CommentHistory,
) -> None:
    result = validate_submission_seal(
        _native(),
        history,
        IssueNodeId(ISSUE_NODE_ID),
        TypeAdapter(Sha256).validate_python("b" * 64),
        {"version": 1},
    )

    assert isinstance(result, ExternalSubmissionDetail)
    assert result.reason == "edited_record"


def test_missing_native_metadata_is_unavailable_not_an_edit() -> None:
    payload = {"version": 1}
    result = validate_submission_seal(
        _native(),
        None,
        IssueNodeId(ISSUE_NODE_ID),
        canonical_payload_digest(payload),
        payload,
    )

    assert isinstance(result, ExternalSubmissionDetail)
    assert result.reason == "seal_unavailable"


def test_changed_wire_body_is_edited_not_author_association() -> None:
    native = _native().model_copy(
        update={"body": '```itree-stage-edit\n{"version":2}\n```\n'}
    )

    result = validate_submission_seal(
        native,
        _history(),
        IssueNodeId(ISSUE_NODE_ID),
        TypeAdapter(Sha256).validate_python("b" * 64),
        {"version": 1},
    )

    assert isinstance(result, ExternalSubmissionDetail)
    assert result.reason == "edited_record"


@pytest.mark.parametrize(
    ("change", "dimension"),
    [
        ({"observed_ref": "feature"}, "wrong_ref"),
        ({"workflow_blob": "stale-blob"}, "stale_workflow"),
        ({"run_attempt": 2}, "mismatched_run_attempt"),
        ({"claimed_human_role": "admin"}, "fake_human_authority"),
    ],
)
def test_writer_authority_failures_keep_their_exact_dimension(
    change: dict[str, str | int | None], dimension: str
) -> None:
    evidence = _authority().model_copy(update=change)

    result = validate_writer_seal(_writer_seal(), evidence)

    assert isinstance(result, WriterSealInvalidContext)
    assert result.failure_dimension == dimension
    assert result.run_id == 101
    assert result.job_id == 201
    assert result.job_node_id == "J_writer"


def test_writer_authority_accepts_only_actual_actions_identity_and_permissions() -> (
    None
):
    result = validate_writer_seal(_writer_seal(), _authority())

    assert result == _writer_seal()


def test_receipt_payload_excludes_native_self_transport_identity() -> None:
    seal = _writer_seal()
    payload = ReceiptPayload(
        version=1,
        issue_node_id=IssueNodeId(ISSUE_NODE_ID),
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="A-receipt",
        attempt_id="attempt-A",
        operation_id="operation-A",
        status="confirmed",
        content={"stage_complete": True, "no_body_write": True},
        writer_seal=seal,
    )
    encoded = payload.model_dump(mode="json")

    assert set(encoded) == {
        "version",
        "issue_node_id",
        "issue_ref",
        "request_id",
        "attempt_id",
        "operation_id",
        "status",
        "content",
        "rejection_reason",
        "rejection_details",
        "indeterminate_reason",
        "writer_seal",
    }
    assert encoded["content"]["high_water_comment_id"] is None
    with pytest.raises(ValidationError):
        TypeAdapter(ReceiptPayload).validate_python({**encoded, "comment_id": 101})

    envelope = ReceiptEnvelope(
        native_issue_comment=_native(), payload=payload, writer_seal=seal
    )
    assert envelope.native_issue_comment.id == 101
    assert envelope.native_issue_comment.node_id == "IC_proposal"


def test_actions_workflow_serializes_nodes_with_least_required_permissions() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/itree-issue-writer.yml").read_text()
    )

    assert workflow["permissions"] == {
        "contents": "read",
        "issues": "write",
        "actions": "read",
    }
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert (
        "github.event.issue.node_id || inputs.issue_node_id"
        in workflow["concurrency"]["group"]
    )
    assert (
        workflow["on"]["workflow_dispatch"]["inputs"]["after_final_read_barrier"][
            "default"
        ]
        is False
    )
    jobs = workflow["jobs"]
    assert "uv run --locked itree stage drain" in jobs["write"]["steps"][-1]["run"]


def test_external_history_unavailable_is_not_a_writer_failure() -> None:
    detail = ExternalSubmissionDetail(
        kind="external_submission",
        actor=ActorLogin("maintainer"),
        reason="seal_unavailable",
    )

    assert not isinstance(detail, WriterSealInvalidContext)
