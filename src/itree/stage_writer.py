from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from .models import (
    ActorLogin,
    AttemptId,
    CommentHistory,
    ExternalSubmissionDetail,
    IssueComment,
    IssueNodeId,
    IssueRef,
    Sha256,
    StageModel,
    SubmissionSeal,
    WriterSeal,
    WriterSealInvalidContext,
)
from .stage import BodyCandidate, ScratchMetadata, StageCommandResult


class WriterAuthorityEvidence(StageModel):
    repository: str
    default_branch: str
    default_head_sha: str
    observed_ref: str
    workflow_path: str
    workflow_blob: str
    workflow_sha256: Sha256
    workflow_permissions: dict[str, str]
    run_id: int
    run_attempt: int
    job_id: int
    job_node_id: str
    job_name: str
    run_url: str
    job_url: str
    actions_installation_identity: str
    operation_log_matches: bool
    claimed_human_role: str | None


JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
STAGE_FENCE = re.compile(
    r"^```(?P<fence>itree-stage-edit|itree-stage-upload-manifest|itree-stage-upload-part|itree-stage-receipt|itree-stage-artifact|itree-stage-artifact-index)\n(?P<payload>[^\n]+)\n```\n$"
)


def canonical_payload_bytes(payload: JsonValue) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_payload_digest(payload: JsonValue) -> Sha256:
    return Sha256(hashlib.sha256(canonical_payload_bytes(payload)).hexdigest())


def stage_comment_body(fence: str, payload: JsonValue) -> str:
    assert STAGE_FENCE.pattern.startswith("^```"), STAGE_FENCE.pattern
    assert fence in {
        "itree-stage-edit",
        "itree-stage-upload-manifest",
        "itree-stage-upload-part",
        "itree-stage-receipt",
        "itree-stage-artifact",
        "itree-stage-artifact-index",
    }, fence
    return f"```{fence}\n{canonical_payload_bytes(payload).decode()}\n```\n"


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_stage_comment(body: str) -> tuple[str, JsonValue]:
    matched = STAGE_FENCE.fullmatch(body)
    if matched is None:
        raise ValueError("stage record must be one deterministic fenced JSON object")
    payload = JSON_VALUE.validate_python(
        json.loads(matched["payload"], object_pairs_hook=_unique_object)
    )
    if stage_comment_body(matched["fence"], payload) != body:
        raise ValueError("stage record is not canonical")
    return matched["fence"], payload


def _external_detail(
    native: IssueComment,
    reason: Literal["seal_unavailable", "edited_record", "role_or_session"],
) -> ExternalSubmissionDetail:
    assert reason in {"seal_unavailable", "edited_record", "role_or_session"}, reason
    return ExternalSubmissionDetail(
        kind="external_submission", actor=ActorLogin(native.author), reason=reason
    )


def validate_submission_seal(
    native: IssueComment,
    history: CommentHistory | None,
    target_issue_node: IssueNodeId,
    logical_payload_sha256: Sha256,
    logical_payload: JsonValue,
) -> SubmissionSeal | ExternalSubmissionDetail:
    if history is None:
        return _external_detail(native, "seal_unavailable")
    exact_wire = hashlib.sha256(native.body.encode("utf-8")).hexdigest()
    if (
        history.created_at != native.created_at
        or history.updated_at != native.updated_at
        or history.created_at != history.updated_at
        or history.last_edited_at is not None
        or history.last_editor is not None
        or history.edit_count != 0
        or not history.history_exhausted
    ):
        return _external_detail(native, "edited_record")
    try:
        _, current_payload = _decode_stage_comment(native.body)
        assert current_payload == logical_payload, (current_payload, logical_payload)
        assert canonical_payload_digest(current_payload) == logical_payload_sha256
    except ValueError, TypeError, ValidationError, AssertionError:
        return _external_detail(native, "edited_record")
    return SubmissionSeal(
        native_issue_comment=native,
        logical_payload_sha256=logical_payload_sha256,
        exact_wire_body_sha256=Sha256(exact_wire),
        history=history,
        target_issue_node=target_issue_node,
    )


def validate_writer_seal(
    seal: WriterSeal, evidence: WriterAuthorityEvidence
) -> WriterSeal | WriterSealInvalidContext:
    context = WriterSealInvalidContext(
        kind="writer_seal_invalid",
        repository=seal.repository,
        expected_default_branch=seal.default_branch,
        observed_ref=evidence.observed_ref,
        expected_workflow_head=seal.default_head_sha,
        observed_workflow_head=evidence.default_head_sha,
        workflow_blob=evidence.workflow_blob,
        run_id=seal.run_id,
        expected_run_attempt=seal.run_attempt,
        observed_run_attempt=evidence.run_attempt,
        job_id=seal.job_id,
        job_node_id=seal.job_node_id,
        failure_dimension="writer_identity_unavailable",
    )
    if evidence.claimed_human_role is not None:
        return context.model_copy(update={"failure_dimension": "fake_human_authority"})
    if (
        evidence.repository != seal.repository
        or evidence.observed_ref != seal.default_branch
        or evidence.default_branch != seal.default_branch
    ):
        return context.model_copy(update={"failure_dimension": "wrong_ref"})
    if (
        evidence.default_head_sha != seal.default_head_sha
        or evidence.workflow_path != seal.workflow_path
        or evidence.workflow_blob != seal.workflow_blob
        or evidence.workflow_sha256 != seal.workflow_sha256
    ):
        return context.model_copy(update={"failure_dimension": "stale_workflow"})
    if evidence.run_attempt != seal.run_attempt:
        return context.model_copy(
            update={"failure_dimension": "mismatched_run_attempt"}
        )
    if evidence.workflow_permissions != {"contents": "read", "issues": "write"}:
        return context.model_copy(
            update={"failure_dimension": "writer_permission_insufficient"}
        )
    if (
        evidence.run_id != seal.run_id
        or evidence.job_id != seal.job_id
        or evidence.job_node_id != seal.job_node_id
        or evidence.job_name != seal.job_name
        or evidence.run_url != seal.run_url
        or evidence.job_url != seal.job_url
        or evidence.actions_installation_identity != seal.actions_installation_identity
        or not evidence.operation_log_matches
    ):
        return context
    return seal


def _pending(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    return StageCommandResult(
        exit_code=5,
        lines=(
            f"issue_ref={issue.slug}",
            f"attempt_id={attempt}",
            "stage_complete=false",
            f"recovery=itree stage resume {issue.slug} --attempt {attempt}",
        ),
    )


def apply_stage_candidate(
    issue: IssueRef,
    attempt: AttemptId,
    feature_directory: Path,
    metadata: ScratchMetadata,
    candidate: BodyCandidate,
) -> StageCommandResult:
    assert feature_directory.is_absolute(), feature_directory
    assert metadata.base_body != candidate.body, metadata.body_sha256
    return _pending(issue, attempt)


def resume_stage(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    return _pending(issue, attempt)


def drain_stage(
    issue: IssueRef, expected_node: IssueNodeId | None
) -> StageCommandResult:
    assert expected_node is None or len(expected_node) > 0, expected_node
    return _pending(issue, AttemptId("drain"))
