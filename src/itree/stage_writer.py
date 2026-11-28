from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

import yaml
from typing import IO, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from .github import GithubApi, GithubApiError, SealHistoryUnavailable
from .models import (
    ActionsJobMetadata,
    ArtifactDraft,
    ArtifactIndexEntry,
    ArtifactPayload,
    ActorLogin,
    AttemptId,
    CommentHistory,
    ExternalSubmissionDetail,
    GithubContentFile,
    GithubIssue,
    IssueComment,
    IssueNodeId,
    IssueRef,
    LegacyIntakeProposal,
    MergeConflictDetail,
    OperationFailureDetail,
    OperationId,
    ReceiptContent,
    ReceiptPayload,
    ReceiptRejectionReason,
    ReceiptStatus,
    RequestId,
    SectionEditProposal,
    SectionReplacement,
    SessionAdmissionRequest,
    SessionId,
    SessionReleaseRequest,
    Sha256,
    StageHttpApplied,
    StageHttpIndeterminate,
    StageHttpOutcome,
    StageHttpRejected,
    StageIndeterminateReason,
    StageModel,
    StageProposal,
    SubmissionSeal,
    TaskDocument,
    TaskFieldProposal,
    UploadManifest,
    UploadPart,
    UploadRejected,
    PendingUpload,
    FinalizedProposal,
    WriterSeal,
    WriterSealInvalidContext,
)
from .stage import (
    BodyCandidate,
    finalize_upload,
    ProjectionFailure,
    _section,
    MergeConflict,
    NoBodyChange,
    ScratchMetadata,
    StageCommandResult,
    StageFailure,
    TaskFieldConflict,
    TaskWireFailure,
    cleanup_stage_scratch,
    collect_artifact_drafts,
    merge_task_documents,
    merge_stage_values,
    parse_task_document,
    replace_task_field,
    replace_sections,
    require_issue_node,
    sha256_text,
)
from .work import (
    LegacyIssueLayout,
    ManagedIssueLayout,
    SectionName,
    contract_fence_inventory,
    parse_issue_layout,
)


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


class DurableRecord(StageModel):
    native: IssueComment
    fence: str
    payload: JsonValue


class DrainReport(StageModel):
    exit_code: int
    confirmed: bool
    lines: tuple[str, ...]
    pending: bool = False


def _writer_enabled() -> bool:
    return (
        os.environ.get("ITREE_STAGE_WRITER_MODE") == "fixture"
        or os.environ.get("GITHUB_ACTIONS") == "true"
    )


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
    if evidence.workflow_permissions != {
        "contents": "read",
        "issues": "write",
        "actions": "read",
    }:
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


def _writer_runtime_seal(api: GithubApi) -> WriterSeal:
    repository = api.get_repository()
    resolution = api.resolve_default_branch()
    job_name = os.environ.get("ITREE_STAGE_JOB_NAME") or os.environ["GITHUB_JOB"]
    run_text = os.environ.get("ITREE_STAGE_RUN_ID") or os.environ["GITHUB_RUN_ID"]
    attempt_text = (
        os.environ.get("ITREE_STAGE_RUN_ATTEMPT") or os.environ["GITHUB_RUN_ATTEMPT"]
    )
    run_id = int(run_text)
    endpoint = f"repos/{api.owner}/{api.repo}/actions/runs/{run_id}/jobs?per_page=100"
    response = api._exec("GET", endpoint)
    raw_jobs = json.loads(response.stdout)["jobs"]
    jobs = TypeAdapter(tuple[ActionsJobMetadata, ...]).validate_python(raw_jobs)
    job = next(job for job in jobs if job.name == job_name)
    content = api.get_repository_content(
        ".github/workflows/itree-issue-writer.yml", job.head_sha
    )
    assert isinstance(content, GithubContentFile), content
    assert content.content is not None, content
    import base64

    workflow = base64.b64decode(content.content)
    parsed_workflow = yaml.safe_load(workflow)
    assert parsed_workflow["permissions"] == {
        "contents": "read",
        "issues": "write",
        "actions": "read",
    }
    assert job.head_branch == repository.default_branch
    assert job.head_sha == resolution.resolved_commit_sha
    workflow_path: Literal[".github/workflows/itree-issue-writer.yml"] = (
        ".github/workflows/itree-issue-writer.yml"
    )
    assert content.path == workflow_path, content.path
    return WriterSeal(
        repository=api.repo_ref.slug,
        default_branch=repository.default_branch,
        default_head_sha=job.head_sha,
        workflow_path=workflow_path,
        workflow_blob=content.sha,
        workflow_sha256=Sha256(hashlib.sha256(workflow).hexdigest()),
        run_id=job.run_id,
        run_attempt=int(attempt_text),
        job_id=job.id,
        job_node_id=job.node_id,
        job_name=job.name,
        run_url=job.run_url,
        job_url=job.html_url,
        actions_installation_identity="github-actions",
        effective_token_permissions={
            "contents": "read",
            "issues": "write",
            "actions": "read",
        },
    )


def _job_authority(api: GithubApi, seal: WriterSeal) -> WriterAuthorityEvidence:
    job = api.get_actions_job(seal.job_id)
    content = api.get_repository_content(seal.workflow_path, job.head_sha)
    assert isinstance(content, GithubContentFile), content
    assert content.content is not None, content
    import base64
    import yaml

    workflow = base64.b64decode(content.content)
    permissions = yaml.safe_load(workflow)["permissions"]
    assert isinstance(permissions, dict), permissions
    return WriterAuthorityEvidence(
        repository=api.repo_ref.slug,
        default_branch=job.head_branch,
        default_head_sha=job.head_sha,
        observed_ref=job.head_branch,
        workflow_path=content.path,
        workflow_blob=content.sha,
        workflow_sha256=Sha256(hashlib.sha256(workflow).hexdigest()),
        workflow_permissions={
            str(key): str(value) for key, value in permissions.items()
        },
        run_id=job.run_id,
        run_attempt=job.run_attempt,
        job_id=job.id,
        job_node_id=job.node_id,
        job_name=job.name,
        run_url=job.run_url,
        job_url=job.html_url,
        actions_installation_identity="github-actions",
        operation_log_matches=True,
        claimed_human_role=None,
    )


def _receipt_for(
    proposal: StageProposal
    | SessionAdmissionRequest
    | SessionReleaseRequest
    | UploadManifest,
    seal: WriterSeal,
    status: ReceiptStatus,
    operation: OperationId | None,
    content: ReceiptContent,
    rejection_reason: ReceiptRejectionReason | None = None,
    rejection_details: MergeConflictDetail
    | OperationFailureDetail
    | ExternalSubmissionDetail
    | WriterSealInvalidContext
    | None = None,
    indeterminate_reason: StageIndeterminateReason | None = None,
) -> ReceiptPayload:
    return ReceiptPayload(
        version=1,
        issue_node_id=proposal.issue_node_id,
        issue_ref=proposal.issue_ref,
        request_id=proposal.request_id,
        attempt_id=proposal.attempt_id,
        operation_id=operation,
        status=status,
        content=content,
        rejection_reason=rejection_reason,
        rejection_details=rejection_details,
        indeterminate_reason=indeterminate_reason,
        writer_seal=seal,
    )


def _model_json(model: StageModel) -> JsonValue:
    return JSON_VALUE.validate_python(model.model_dump(mode="json"))


def _post_record(
    api: GithubApi, issue: IssueRef, fence: str, payload: StageModel
) -> IssueComment | StageHttpRejected | StageHttpIndeterminate:
    outcome = api.post_stage_comment(
        issue.number, stage_comment_body(fence, _model_json(payload))
    )
    if isinstance(outcome, StageHttpRejected | StageHttpIndeterminate):
        return outcome
    try:
        return IssueComment.model_validate_json(outcome.response_body)
    except ValidationError:
        return StageHttpIndeterminate(
            reason="unusable_success_response",
            diagnostic="comment creation returned unusable native JSON",
        )


def _all_records(api: GithubApi, issue: IssueRef) -> tuple[DurableRecord, ...]:
    records: list[DurableRecord] = []
    for native in sorted(api.list_comments(issue.number), key=lambda item: item.id):
        try:
            fence, payload = _decode_stage_comment(native.body)
        except ValueError, TypeError, ValidationError, json.JSONDecodeError:
            if native.body.startswith("```itree-stage-"):
                records.append(
                    DurableRecord(
                        native=native,
                        fence=native.body.split("\n", 1)[0].removeprefix("```"),
                        payload={"malformed": True},
                    )
                )
            continue
        records.append(DurableRecord(native=native, fence=fence, payload=payload))
    return tuple(records)


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


def _require_live_issue(
    api: GithubApi, ref: IssueRef, expected_node: IssueNodeId | None
) -> GithubIssue:
    live = api.get_issue(ref.number)
    if live.repository_slug != ref.repo_ref.slug:
        raise StageFailure(
            ProjectionFailure(
                reason="wrong_repository",
                exit_code=3,
                detail=live.repository_slug,
            )
        )
    node = require_issue_node(live)
    if expected_node is not None and node != expected_node:
        raise StageFailure(
            ProjectionFailure(
                reason="node_mismatch",
                exit_code=4,
                detail=f"expected={expected_node}; observed={node}",
            )
        )
    return live


def _layout(body: str) -> ManagedIssueLayout | LegacyIssueLayout:
    layout = parse_issue_layout(body)
    if layout.kind == "invalid":
        first = layout.errors[0]
        raise StageFailure(
            ProjectionFailure(
                reason="invalid_layout",
                exit_code=4,
                detail=f"{first.name}:{first.line}:{first.reason}",
            )
        )
    return layout


def _operation(request: RequestId) -> OperationId:
    return OperationId("operation-" + request)


def _fence_sha(body: str) -> Sha256:
    return sha256_text(
        "\n".join(fence.text for fence in contract_fence_inventory(body))
    )


def _raw_history(api: GithubApi, native: IssueComment) -> CommentHistory | None:
    try:
        current = api.get_comment(native.id)
        history = api.get_comment_history(current.node_id)
    except (
        GithubApiError,
        SealHistoryUnavailable,
        KeyError,
        ValidationError,
        AssertionError,
        TypeError,
        ValueError,
    ):
        return None
    return history.model_copy(
        update={"created_at": current.created_at, "updated_at": current.updated_at}
    )


def _external_authorized(
    api: GithubApi,
    native: IssueComment,
    payload: StageModel,
    issue: GithubIssue,
    enrolled: tuple[ActorLogin, ...],
    maintainer_only: bool,
) -> SubmissionSeal | ExternalSubmissionDetail:
    current = api.get_comment(native.id)
    history = _raw_history(api, current)
    if history is None:
        return _external_detail(current, "seal_unavailable")
    logical = _model_json(payload)
    sealed = validate_submission_seal(
        current,
        history,
        require_issue_node(issue),
        canonical_payload_digest(logical),
        logical,
    )
    if isinstance(sealed, ExternalSubmissionDetail):
        return sealed
    try:
        permission = api.get_collaborator_permission(current.author)
    except GithubApiError, ValidationError, KeyError:
        return _external_detail(current, "seal_unavailable")
    roles = (
        {"maintain", "admin"}
        if maintainer_only
        else {"triage", "write", "maintain", "admin"}
    )
    if permission.permission not in roles or ActorLogin(current.author) not in enrolled:
        return _external_detail(current, "role_or_session")
    return sealed


class UploadDrain(StageModel):
    proposals: tuple[StageProposal, ...]
    pending: tuple[str, ...]
    failed: tuple[str, ...]


def _upload_models(records: tuple[DurableRecord, ...]) -> tuple[UploadManifest, ...]:
    return tuple(
        UploadManifest.model_validate_json(
            canonical_payload_bytes(record.payload), strict=True
        )
        for record in records
        if record.fence == "itree-stage-upload-manifest"
    )


def _upload_part_models(
    records: tuple[DurableRecord, ...], manifest: UploadManifest
) -> tuple[UploadPart, ...]:
    return tuple(
        UploadPart.model_validate_json(
            canonical_payload_bytes(record.payload), strict=True
        )
        for record in records
        if record.fence == "itree-stage-upload-part"
        and isinstance(record.payload, dict)
        and record.payload.get("manifest_request_id") == manifest.request_id
    )


def _native_upload_part(
    records: tuple[DurableRecord, ...], part: UploadPart
) -> IssueComment:
    identity = (part.role, part.ordinal)
    return next(
        record.native
        for record in records
        if record.fence == "itree-stage-upload-part"
        and isinstance(record.payload, dict)
        and (record.payload.get("role"), record.payload.get("ordinal")) == identity
        and record.payload.get("manifest_request_id") == part.manifest_request_id
    )


def _upload_proposals(
    api: GithubApi,
    issue: IssueRef,
    records: tuple[DurableRecord, ...],
    live: GithubIssue,
    sessions: dict[SessionId, SessionAdmissionRequest],
) -> UploadDrain:
    proposals: list[StageProposal] = []
    pending: list[str] = []
    failed: list[str] = []
    manifests = _upload_models(records)
    for manifest in manifests:
        session = sessions.get(manifest.session_id)
        if session is None:
            pending.append(f"{manifest.request_id}:editor_not_enrolled")
            continue
        native = next(
            record.native
            for record in records
            if record.fence == "itree-stage-upload-manifest"
            and isinstance(record.payload, dict)
            and record.payload.get("request_id") == manifest.request_id
        )
        approved = _external_authorized(
            api, native, manifest, live, session.enrolled_actors, False
        )
        if isinstance(approved, ExternalSubmissionDetail):
            failed.append(f"{manifest.request_id}:{approved.reason}")
            continue
        sealed_parts: list[UploadPart] = []
        invalid: ExternalSubmissionDetail | None = None
        for part in _upload_part_models(records, manifest):
            native_part = _native_upload_part(records, part)
            checked = _external_authorized(
                api, native_part, part, live, session.enrolled_actors, False
            )
            if isinstance(checked, ExternalSubmissionDetail):
                invalid = checked
                break
            sealed_parts.append(part)
        if invalid is not None:
            failed.append(f"{manifest.request_id}:{invalid.reason}")
            continue
        state = finalize_upload(manifest, tuple(sealed_parts))
        if isinstance(state, PendingUpload):
            missing = ",".join(
                f"{part.role}:{part.ordinal}" for part in state.missing_parts
            )
            pending.append(f"{manifest.request_id}:pending_upload:{missing}")
            continue
        if isinstance(state, UploadRejected):
            failed.append(f"{manifest.request_id}:{state.reason}")
            continue
        assert isinstance(state, FinalizedProposal)
        if manifest.request_kind in {"session_admit", "session_release"}:
            continue
        proposals.append(
            TypeAdapter(StageProposal).validate_json(
                canonical_payload_bytes(state.proposal), strict=True
            )
        )
    known_manifest_requests = {manifest.request_id for manifest in manifests}
    for record in records:
        if record.fence != "itree-stage-upload-part" or not isinstance(
            record.payload, dict
        ):
            continue
        if record.payload.get("manifest_request_id") not in known_manifest_requests:
            pending.append("unknown:manifest_required")
    return UploadDrain(
        proposals=tuple(proposals),
        pending=tuple(pending),
        failed=tuple(failed),
    )


def _proposal_models(records: tuple[DurableRecord, ...]) -> tuple[StageProposal, ...]:
    proposals: list[StageProposal] = []
    for record in records:
        if record.fence != "itree-stage-edit" or not isinstance(record.payload, dict):
            continue
        if record.payload.get("request_kind") not in {
            "sections",
            "task_field",
            "legacy_intake",
        }:
            continue
        proposals.append(
            TypeAdapter(StageProposal).validate_json(
                canonical_payload_bytes(record.payload), strict=True
            )
        )
    return tuple(proposals)


def _session_models(
    records: tuple[DurableRecord, ...],
) -> tuple[SessionAdmissionRequest, ...]:
    sessions = [
        SessionAdmissionRequest.model_validate_json(
            canonical_payload_bytes(record.payload), strict=True
        )
        for record in records
        if record.fence == "itree-stage-edit"
        and isinstance(record.payload, dict)
        and record.payload.get("request_kind") == "session_admit"
    ]
    for manifest in _upload_models(records):
        if manifest.request_kind != "session_admit":
            continue
        finalized = finalize_upload(manifest, _upload_part_models(records, manifest))
        if isinstance(finalized, FinalizedProposal):
            sessions.append(
                SessionAdmissionRequest.model_validate_json(
                    canonical_payload_bytes(finalized.proposal), strict=True
                )
            )
    return tuple(sessions)


def _release_models(
    records: tuple[DurableRecord, ...],
) -> tuple[SessionReleaseRequest, ...]:
    return tuple(
        SessionReleaseRequest.model_validate_json(
            canonical_payload_bytes(record.payload), strict=True
        )
        for record in records
        if record.fence == "itree-stage-edit"
        and isinstance(record.payload, dict)
        and record.payload.get("request_kind") == "session_release"
    )


def _receipt_by_request(
    records: tuple[DurableRecord, ...],
) -> dict[RequestId, ReceiptPayload]:
    receipts: dict[RequestId, ReceiptPayload] = {}
    for record in records:
        if record.fence == "itree-stage-receipt":
            payload = ReceiptPayload.model_validate_json(
                canonical_payload_bytes(record.payload), strict=True
            )
            receipts[payload.request_id] = payload
    return receipts


def _native_for_proposal(
    records: tuple[DurableRecord, ...], request: RequestId
) -> DurableRecord:
    return next(
        record
        for record in records
        if record.fence in {"itree-stage-edit", "itree-stage-upload-manifest"}
        and isinstance(record.payload, dict)
        and record.payload.get("request_id") == request
    )


def _terminal(
    api: GithubApi,
    issue: IssueRef,
    payload: ReceiptPayload,
    seal: WriterSeal,
) -> (
    IssueComment | StageHttpRejected | StageHttpIndeterminate | WriterSealInvalidContext
):
    posted = _post_record(api, issue, "itree-stage-receipt", payload)
    if not isinstance(posted, IssueComment):
        return posted
    try:
        current = api.get_comment(posted.id)
        history = _raw_history(api, current)
        assert history is not None
        assert history.created_at == history.updated_at
        assert history.last_edited_at is None
        assert history.last_editor is None
        assert history.edit_count == 0
        assert history.history_exhausted
        assert current.performed_via_github_app is not None
        assert current.performed_via_github_app.slug == "github-actions"
        evidence = _job_authority(api, seal)
    except (
        GithubApiError,
        SealHistoryUnavailable,
        AssertionError,
        ValidationError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return StageHttpIndeterminate(
            reason="receipt_unavailable",
            diagnostic="writer terminal readback could not prove its native seal",
        )
    validated = validate_writer_seal(seal, evidence)
    if isinstance(validated, WriterSealInvalidContext):
        return validated
    return current


def _node_lock(issue_node: IssueNodeId) -> IO[bytes]:
    root_text = os.environ.get("ITREE_STAGE_FIXTURE_ROOT")
    root = (
        Path(root_text)
        if root_text is not None
        else Path("/tmp/opencode/itree-stage-locks")
    )
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(issue_node.encode()).hexdigest()
    handle = root.joinpath(key + ".lock").open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _unlock(handle: IO[bytes]) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _current_artifact_index(plan_text: str) -> tuple[ArtifactIndexEntry, ...]:
    matched = re.search(r"```itree-stage-artifact-index\n[^\n]+\n```\n", plan_text)
    if matched is None:
        return ()
    _, payload = _decode_stage_comment(matched.group(0))
    return TypeAdapter(tuple[ArtifactIndexEntry, ...]).validate_json(
        matched.group(0).splitlines()[1], strict=True
    )


def _artifact_comment(
    api: GithubApi, issue: IssueRef, payload: ArtifactPayload
) -> (
    IssueComment | StageHttpRejected | StageHttpIndeterminate | WriterSealInvalidContext
):
    posted = _post_record(api, issue, "itree-stage-artifact", payload)
    if not isinstance(posted, IssueComment):
        return posted
    try:
        current = api.get_comment(posted.id)
        history = _raw_history(api, current)
        assert history is not None
        assert history.created_at == history.updated_at
        assert history.last_edited_at is None
        assert history.last_editor is None
        assert history.edit_count == 0
        assert history.history_exhausted
        assert current.performed_via_github_app is not None
        assert current.performed_via_github_app.slug == "github-actions"
        evidence = _job_authority(api, payload.writer_seal)
        checked = validate_writer_seal(payload.writer_seal, evidence)
        assert checked == payload.writer_seal
        assert current.body == stage_comment_body(
            "itree-stage-artifact", _model_json(payload)
        )
    except (
        GithubApiError,
        SealHistoryUnavailable,
        AssertionError,
        ValidationError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return StageHttpIndeterminate(
            reason="receipt_unavailable",
            diagnostic="artifact native readback or writer seal could not be verified",
        )
    return current


def _publish_artifacts(
    api: GithubApi,
    issue: IssueRef,
    records: tuple[DurableRecord, ...],
    proposal: SectionEditProposal,
    seal: WriterSeal,
) -> tuple[ArtifactIndexEntry, ...]:
    published: list[ArtifactIndexEntry] = []
    for draft in proposal.artifacts:
        payload = ArtifactPayload(
            version=1,
            issue_node_id=proposal.issue_node_id,
            issue_ref=proposal.issue_ref,
            request_id=proposal.request_id,
            attempt_id=proposal.attempt_id,
            key=draft.key,
            kind=draft.kind,
            payload_sha256=sha256_text(draft.content),
            content=draft.content,
            writer_seal=seal,
        )
        expected_body = stage_comment_body("itree-stage-artifact", _model_json(payload))
        existing = next(
            (
                record
                for record in records
                if record.fence == "itree-stage-artifact"
                and isinstance(record.payload, dict)
                and record.payload.get("request_id") == proposal.request_id
                and record.payload.get("key") == draft.key
                and record.payload.get("payload_sha256") == payload.payload_sha256
                and record.native.body == expected_body
            ),
            None,
        )
        if existing is None:
            native = _artifact_comment(api, issue, payload)
            if not isinstance(native, IssueComment):
                raise StageFailure(
                    ProjectionFailure(
                        reason="issue_unavailable",
                        exit_code=5,
                        detail=f"unconfirmed artifact {draft.key}",
                    )
                )
        else:
            native = existing.native
            history = _raw_history(api, native)
            assert history is not None and history.history_exhausted
            assert history.last_edited_at is None and history.edit_count == 0
            evidence = _job_authority(api, payload.writer_seal)
            assert (
                validate_writer_seal(payload.writer_seal, evidence)
                == payload.writer_seal
            )
        published.append(
            ArtifactIndexEntry(
                key=draft.key,
                kind=draft.kind,
                comment_id=native.id,
                comment_node_id=native.node_id,
                url=native.html_url,
                content_sha256=payload.payload_sha256,
                attempt_id=proposal.attempt_id,
            )
        )
    return tuple(published)


def _candidate_with_artifacts(
    candidate: BodyCandidate,
    current_plan_text: str,
    published: tuple[ArtifactIndexEntry, ...],
) -> BodyCandidate:
    if not published and "itree-stage-artifact-index" not in current_plan_text:
        return candidate
    preserved = [
        entry
        for entry in _current_artifact_index(current_plan_text)
        if entry.key not in {artifact.key for artifact in published}
    ]
    entries = tuple(
        sorted(
            (*preserved, *published),
            key=lambda entry: (entry.kind, entry.key),
        )
    )
    index_body = stage_comment_body(
        "itree-stage-artifact-index",
        [entry.model_dump(mode="json") for entry in entries],
    )
    matched = re.search(
        r"```itree-stage-artifact-index\n[^\n]+\n```\n", current_plan_text
    )
    without_index = (
        current_plan_text
        if matched is None
        else current_plan_text[: matched.start()] + current_plan_text[matched.end() :]
    )
    proposed_plan = without_index + index_body
    if proposed_plan == current_plan_text:
        return candidate
    layout = _layout(candidate.body)
    assert isinstance(layout, ManagedIssueLayout)
    replacement = SectionReplacement(
        section="plan",
        base_text=current_plan_text,
        proposed_text=proposed_plan,
        base_sha256=sha256_text(current_plan_text),
        proposed_sha256=sha256_text(proposed_plan),
    )
    changed = replace_sections(layout, (replacement,))
    assert isinstance(changed, BodyCandidate), changed
    return changed


def _receipt_is_current(
    api: GithubApi, record: DurableRecord, payload: ReceiptPayload
) -> bool:
    try:
        native = api.get_comment(record.native.id)
        history = _raw_history(api, native)
        assert history is not None
        assert history.created_at == history.updated_at
        assert history.last_edited_at is None and history.last_editor is None
        assert history.edit_count == 0 and history.history_exhausted
        assert native.body == stage_comment_body(
            "itree-stage-receipt", _model_json(payload)
        )
        assert native.performed_via_github_app is not None
        assert native.performed_via_github_app.slug == "github-actions"
        return (
            validate_writer_seal(
                payload.writer_seal, _job_authority(api, payload.writer_seal)
            )
            == payload.writer_seal
        )
    except (
        GithubApiError,
        SealHistoryUnavailable,
        AssertionError,
        ValidationError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def _merge_task_field_proposal(
    base: ManagedIssueLayout,
    current: ManagedIssueLayout,
    proposal: TaskFieldProposal,
) -> BodyCandidate | MergeConflict:
    base_tasks_text = _section(base, SectionName.tasks)
    current_tasks_text = _section(current, SectionName.tasks)
    base_document = parse_task_document(base_tasks_text)
    if isinstance(base_document, TaskWireFailure) or not base_document.wrapped:
        return MergeConflict(section="tasks", retained_body=current.body)
    base_task = next(
        (task for task in base_document.tasks if task.id == proposal.task_id), None
    )
    if (
        base_task is None
        or sha256_text(base_task.source) != proposal.full_task_base_sha256
    ):
        return MergeConflict(
            section="tasks",
            task_id=proposal.task_id,
            field=proposal.field,
            retained_body=current.body,
        )
    proposed_document = replace_task_field(
        base_document, proposal.task_id, proposal.field, proposal.proposed_value
    )
    if isinstance(proposed_document, TaskWireFailure):
        return MergeConflict(
            section="tasks",
            task_id=proposal.task_id,
            field=proposal.field,
            retained_body=current.body,
        )
    current_document = parse_task_document(current_tasks_text)
    if isinstance(current_document, TaskWireFailure):
        return MergeConflict(section="tasks", retained_body=current.body)
    merged = merge_task_documents(base_document, current_document, proposed_document)
    if isinstance(merged, TaskFieldConflict):
        return MergeConflict(
            section="tasks",
            task_id=merged.task_id,
            field=merged.field,
            retained_body=current.body,
        )
    replacement = SectionReplacement(
        section="tasks",
        base_text=current_tasks_text,
        proposed_text=merged.source,
        base_sha256=sha256_text(current_tasks_text),
        proposed_sha256=sha256_text(merged.source),
    )
    return merge_stage_values(base, current, (replacement,))


def _dispatch_pending(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    api = GithubApi.from_issue_ref(issue)
    live = api.get_issue(issue.number)
    try:
        api.dispatch_stage_drain(require_issue_node(live), issue.number)
    except GithubApiError as error:
        return StageCommandResult(
            exit_code=5,
            lines=(
                f"issue_ref={issue.slug}",
                f"attempt_id={attempt}",
                "stage_complete=false",
                f"dispatch_outcome=unconfirmed:{error.status}",
                f"recovery=itree stage resume {issue.slug} --attempt {attempt}",
            ),
        )
    return _pending(issue, attempt)


def _release_barrier(
    api: GithubApi,
    issue: IssueRef,
    request: RequestId,
    operation: OperationId,
    seal: WriterSeal,
    root: Path,
) -> None:
    checkpoint = {
        "kind": "itree-stage-barrier-checkpoint",
        "request_id": request,
        "operation_id": operation,
        "run_id": seal.run_id,
        "run_attempt": seal.run_attempt,
        "job_id": seal.job_id,
        "job_node_id": seal.job_node_id,
        "run_url": seal.run_url,
        "position": "after_final_get_before_intent_and_patch",
    }
    root.joinpath("checkpoint.json").write_text(json.dumps(checkpoint))
    root.joinpath("controller-trace.json").write_text(
        json.dumps({"b_may_not_patch": True})
    )
    control_repo = os.environ.get("ITREE_STAGE_BARRIER_CONTROL_REPO")
    if control_repo is None:
        while not root.joinpath("release.json").exists():
            time.sleep(0.02)
        return
    assert control_repo == issue.repo_ref.slug, (control_repo, issue.repo_ref.slug)
    api.add_comment(issue.number, json.dumps(checkpoint, sort_keys=True))
    while True:
        for comment in api.list_comments(issue.number):
            if not comment.body.startswith("{"):
                continue
            try:
                control = json.loads(comment.body)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(control, dict)
                or control.get("kind") != "itree-stage-barrier-release"
            ):
                continue
            if control.get("request_id") != request:
                continue
            records = _all_records(api, issue)
            has_durable_b = any(
                isinstance(record.payload, dict)
                and record.payload.get("request_id") == control.get("b_request_id")
                and record.native.id == control.get("b_comment_id")
                for record in records
            )
            if has_durable_b:
                root.joinpath("release.json").write_text(comment.body)
                return
        time.sleep(0.1)


def _drain_node(
    issue: IssueRef, expected_node: IssueNodeId | None, attempt: AttemptId | None
) -> DrainReport:
    api = GithubApi.from_issue_ref(issue)
    live = _require_live_issue(api, issue, expected_node)
    seal = _writer_runtime_seal(api)
    checked_seal = validate_writer_seal(seal, _job_authority(api, seal))
    if isinstance(checked_seal, WriterSealInvalidContext):
        return DrainReport(
            exit_code=4,
            confirmed=False,
            lines=(f"reason=writer_seal_invalid:{checked_seal.failure_dimension}",),
        )
    lock = _node_lock(require_issue_node(live))
    try:
        records = _all_records(api, issue)
        for record in records:
            if not isinstance(record.payload, dict) or not record.payload.get(
                "malformed"
            ):
                continue
            history = _raw_history(api, record.native)
            if history is None:
                return DrainReport(
                    exit_code=3,
                    confirmed=False,
                    lines=("reason=seal_unavailable",),
                )
            edited = (
                history.created_at != history.updated_at
                or history.last_edited_at is not None
                or history.last_editor is not None
                or history.edit_count != 0
                or not history.history_exhausted
            )
            reason = "edited_record" if edited else "malformed_payload"
            return DrainReport(
                exit_code=4,
                confirmed=False,
                lines=(f"reason={reason}", f"comment_id={record.native.id}"),
            )
        receipts = _receipt_by_request(records)
        sessions = {
            session.session_id: session
            for session in _session_models(records)
            if receipts.get(session.request_id) is not None
            and receipts[session.request_id].status == "session_admitted"
        }
        pending: list[str] = []
        failed: list[str] = []
        last_applied_sha = next(
            (
                receipt.content.live_body_sha256
                for receipt in reversed(tuple(receipts.values()))
                if receipt.status == "confirmed"
                and receipt.content.live_body_sha256 is not None
            ),
            None,
        )
        prefix: list[str] = []
        lines: list[str] = []
        for session in _session_models(records):
            if attempt is not None and session.attempt_id != attempt:
                continue
            if session.request_id in receipts:
                continue
            session_record = _find_record_native(records, session.request_id)
            session_payload: StageModel = session
            if session_record.fence == "itree-stage-upload-manifest":
                session_payload = UploadManifest.model_validate_json(
                    canonical_payload_bytes(session_record.payload), strict=True
                )
            approved = _external_authorized(
                api,
                session_record.native,
                session_payload,
                live,
                session.enrolled_actors,
                True,
            )
            if (
                isinstance(approved, SubmissionSeal)
                and session_record.fence == "itree-stage-upload-manifest"
            ):
                session_manifest = next(
                    manifest
                    for manifest in _upload_models(records)
                    if manifest.request_id == session.request_id
                )
                for part in _upload_part_models(records, session_manifest):
                    part_native = _native_upload_part(records, part)
                    checked = _external_authorized(
                        api,
                        part_native,
                        part,
                        live,
                        session.enrolled_actors,
                        True,
                    )
                    if isinstance(checked, ExternalSubmissionDetail):
                        approved = checked
                        break
            if isinstance(approved, ExternalSubmissionDetail):
                rejected = _receipt_for(
                    session,
                    seal,
                    "rejected",
                    None,
                    ReceiptContent(live_body_sha256=sha256_text(live.body or "")),
                    "external_submission",
                    approved,
                )
                posted = _terminal(api, issue, rejected, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                lines.append(
                    f"rejected={session.request_id}:{posted.id}:{approved.reason}"
                )
                failed.append(session.request_id)
                continue
            admitted = _receipt_for(
                session,
                seal,
                "session_admitted",
                None,
                ReceiptContent(
                    stage_complete=True, live_body_sha256=session.entry_body_sha256
                ),
            )
            posted = _terminal(api, issue, admitted, seal)
            if not isinstance(posted, IssueComment):
                return DrainReport(
                    exit_code=5, confirmed=False, lines=("receipt_unavailable=true",)
                )
            sessions[session.session_id] = session
            prefix.append(session.request_id)
            lines.append(f"session_admitted={session.session_id}:{posted.id}")
        upload_drain = _upload_proposals(api, issue, records, live, sessions)
        pending.extend(upload_drain.pending)
        failed.extend(upload_drain.failed)
        proposal_digests: dict[RequestId, Sha256] = {}
        for proposal in (*_proposal_models(records), *upload_drain.proposals):
            if attempt is not None and proposal.attempt_id != attempt:
                continue
            request = proposal.request_id
            native_record = _native_for_proposal(records, request)
            digest = canonical_payload_digest(native_record.payload)
            previous_digest = proposal_digests.get(request)
            if previous_digest is not None and previous_digest != digest:
                lines.append(f"rejected={request}:duplicate_payload_conflict")
                failed.append(request)
                continue
            proposal_digests[request] = digest
            existing = receipts.get(request)
            managed_session = sessions.get(proposal.session_id)
            if managed_session is None:
                pending.append(request)
                lines.append(f"pending={request}:editor_not_enrolled")
                continue
            admission_payload: StageModel = proposal
            if native_record.fence == "itree-stage-upload-manifest":
                admission_payload = UploadManifest.model_validate_json(
                    canonical_payload_bytes(native_record.payload), strict=True
                )
            approved = _external_authorized(
                api,
                native_record.native,
                admission_payload,
                live,
                managed_session.enrolled_actors,
                False,
            )
            if isinstance(approved, ExternalSubmissionDetail):
                rejected = _receipt_for(
                    proposal,
                    seal,
                    "rejected",
                    _operation(request),
                    ReceiptContent(live_body_sha256=sha256_text(live.body or "")),
                    "external_submission",
                    approved,
                )
                posted = _terminal(api, issue, rejected, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                lines.append(f"rejected={request}:{posted.id}:{approved.reason}")
                failed.append(request)
                continue
            if existing is not None:
                receipt_record = next(
                    record
                    for record in reversed(records)
                    if record.fence == "itree-stage-receipt"
                    and isinstance(record.payload, dict)
                    and record.payload.get("request_id") == request
                )
                if not _receipt_is_current(api, receipt_record, existing):
                    invalidated = _receipt_for(
                        proposal,
                        seal,
                        "seal_invalidated",
                        _operation(request),
                        ReceiptContent(
                            live_body_sha256=sha256_text(live.body or ""),
                            recovery_action=f"itree stage resume {issue.slug} --attempt {proposal.attempt_id}",
                        ),
                    )
                    posted = _terminal(api, issue, invalidated, seal)
                    if not isinstance(posted, IssueComment):
                        return DrainReport(
                            exit_code=5,
                            confirmed=False,
                            lines=("receipt_unavailable=true",),
                        )
                    lines.append(f"rejected={request}:{posted.id}:seal_invalidated")
                    failed.append(request)
                    continue
                if existing.status == "confirmed":
                    prefix.append(request)
                    assert existing.content.live_body_sha256 is not None
                    last_applied_sha = existing.content.live_body_sha256
                    lines.append(f"confirmed={request}:replay")
                    continue
                if existing.status == "rejected":
                    lines.append(f"rejected={request}:replay")
                    failed.append(request)
                    continue
                if existing.status in {"intended", "indeterminate"}:
                    assert existing.content.candidate_body_sha256 is not None
                    live = _require_live_issue(api, issue, expected_node)
                    if (
                        sha256_text(live.body or "")
                        == existing.content.candidate_body_sha256
                    ):
                        recovered = _receipt_for(
                            proposal,
                            seal,
                            "confirmed",
                            _operation(request),
                            ReceiptContent(
                                candidate_body_sha256=existing.content.candidate_body_sha256,
                                live_body_sha256=sha256_text(live.body or ""),
                                original_sha256=existing.content.original_sha256,
                                fence_sha256=existing.content.fence_sha256,
                                no_body_write=False,
                                stage_complete=True,
                            ),
                        )
                        posted = _terminal(api, issue, recovered, seal)
                        if not isinstance(posted, IssueComment):
                            return DrainReport(
                                exit_code=5,
                                confirmed=False,
                                lines=("receipt_unavailable=true",),
                            )
                        prefix.append(request)
                        last_applied_sha = sha256_text(live.body or "")
                        lines.append(f"confirmed={request}:{posted.id}:recovered")
                        continue
                pending.append(request)
                lines.append(f"indeterminate={request}")
                continue
            live = _require_live_issue(api, issue, expected_node)
            observed_sha = sha256_text(live.body or "")
            required_sha = last_applied_sha or managed_session.entry_body_sha256
            if observed_sha != required_sha:
                rejected = _receipt_for(
                    proposal,
                    seal,
                    "rejected",
                    _operation(request),
                    ReceiptContent(live_body_sha256=observed_sha),
                    "candidate_changed",
                    OperationFailureDetail(
                        kind="operation_failure",
                        reason="candidate_changed",
                        diagnostic=f"direct body edit: expected={required_sha}; observed={observed_sha}",
                    ),
                )
                posted = _terminal(api, issue, rejected, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                lines.append(f"rejected={request}:{posted.id}:external_mutation")
                failed.append(request)
                continue
            candidate: BodyCandidate | MergeConflict
            try:
                base = _layout(
                    proposal.legacy_base
                    if isinstance(proposal, LegacyIntakeProposal)
                    else proposal.base_body
                )
                current = _layout(live.body or "")
                if isinstance(proposal, LegacyIntakeProposal):
                    if (
                        not isinstance(base, LegacyIssueLayout)
                        or not isinstance(current, LegacyIssueLayout)
                        or base.body != current.body
                    ):
                        candidate = MergeConflict(
                            section="original-contract", retained_body=current.body
                        )
                    elif not any(
                        (proposal.requirements, proposal.plan, proposal.tasks)
                    ):
                        candidate = BodyCandidate(
                            body=base.body,
                            body_sha256=sha256_text(base.body),
                            original_sha256=sha256_text(base.original_contract),
                            changed_sections=(),
                            before_fences=tuple(
                                fence.text
                                for fence in contract_fence_inventory(base.body)
                            ),
                            after_fences=tuple(
                                fence.text
                                for fence in contract_fence_inventory(base.body)
                            ),
                        )
                    else:
                        from .stage import build_legacy_intake

                        candidate = build_legacy_intake(
                            base, proposal.requirements, proposal.plan, proposal.tasks
                        )
                elif isinstance(proposal, TaskFieldProposal):
                    assert isinstance(base, ManagedIssueLayout) and isinstance(
                        current, ManagedIssueLayout
                    )
                    candidate = _merge_task_field_proposal(base, current, proposal)
                else:
                    assert isinstance(base, ManagedIssueLayout) and isinstance(
                        current, ManagedIssueLayout
                    )
                    candidate = merge_stage_values(base, current, proposal.changes)
            except StageFailure as error:
                return DrainReport(
                    exit_code=error.failure.exit_code,
                    confirmed=False,
                    lines=(f"reason={error.failure.reason}", error.failure.detail),
                )
            if isinstance(candidate, MergeConflict):
                rejected = _receipt_for(
                    proposal,
                    seal,
                    "rejected",
                    _operation(request),
                    ReceiptContent(live_body_sha256=sha256_text(live.body or "")),
                    "conflict",
                    MergeConflictDetail(
                        kind="merge_conflict",
                        section=candidate.section,
                        task_id=candidate.task_id,
                        field=candidate.field,
                        competing_request_id=candidate.competing_request_id,
                    ),
                )
                posted = _terminal(api, issue, rejected, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                lines.append(
                    f"rejected={request}:{posted.id}:conflict:{candidate.section}"
                )
                failed.append(request)
                continue
            operation = _operation(request)
            configured = os.environ.get("ITREE_STAGE_BARRIER_REQUEST")
            if configured == request:
                root_text = os.environ.get("ITREE_STAGE_FIXTURE_ROOT")
                assert root_text is not None, "barrier requires explicit fixture root"
                root = Path(root_text)
                root.mkdir(parents=True, exist_ok=True)
                _release_barrier(api, issue, request, operation, seal, root)
            final_approved = _external_authorized(
                api,
                native_record.native,
                admission_payload,
                live,
                managed_session.enrolled_actors,
                False,
            )
            if isinstance(final_approved, ExternalSubmissionDetail):
                rejected = _receipt_for(
                    proposal,
                    seal,
                    "rejected",
                    operation,
                    ReceiptContent(live_body_sha256=sha256_text(live.body or "")),
                    "external_submission",
                    final_approved,
                )
                posted = _terminal(api, issue, rejected, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                lines.append(f"rejected={request}:{posted.id}:{final_approved.reason}")
                failed.append(request)
                continue
            published: tuple[ArtifactIndexEntry, ...] = ()
            if isinstance(proposal, SectionEditProposal):
                published = _publish_artifacts(api, issue, records, proposal, seal)
                current_layout = _layout(candidate.body)
                assert isinstance(current_layout, ManagedIssueLayout)
                candidate = _candidate_with_artifacts(
                    candidate,
                    _section(current_layout, SectionName.plan),
                    published,
                )
            final_layout = _layout(candidate.body)
            assert isinstance(final_layout, ManagedIssueLayout)
            indexed_entries = _current_artifact_index(
                _section(final_layout, SectionName.plan)
            )
            if candidate.body == live.body:
                confirmed = _receipt_for(
                    proposal,
                    seal,
                    "confirmed",
                    operation,
                    ReceiptContent(
                        candidate_body_sha256=candidate.body_sha256,
                        live_body_sha256=sha256_text(live.body or ""),
                        original_sha256=candidate.original_sha256,
                        fence_sha256=_fence_sha(live.body or ""),
                        no_body_write=True,
                        stage_complete=True,
                        artifacts=indexed_entries,
                    ),
                )
                posted = _terminal(api, issue, confirmed, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                prefix.append(request)
                last_applied_sha = sha256_text(live.body or "")
                lines.append(f"confirmed={request}:{posted.id}:no_body_write:true")
                continue
            intent = _receipt_for(
                proposal,
                seal,
                "intended",
                operation,
                ReceiptContent(
                    base_body_sha256=sha256_text(live.body or ""),
                    candidate_body_sha256=candidate.body_sha256,
                    original_sha256=candidate.original_sha256,
                    fence_sha256=_fence_sha(live.body or ""),
                    artifacts=indexed_entries,
                ),
            )
            posted_intent = _post_record(api, issue, "itree-stage-receipt", intent)
            if not isinstance(posted_intent, IssueComment):
                pending.append(request)
                lines.append(f"indeterminate={request}:intent_unavailable")
                break
            outcome = api.patch_stage_body(issue.number, candidate.body)
            if isinstance(outcome, StageHttpRejected):
                rejected = _receipt_for(
                    proposal,
                    seal,
                    "rejected",
                    operation,
                    ReceiptContent(live_body_sha256=sha256_text(live.body or "")),
                    "github_http_rejection",
                    OperationFailureDetail(
                        kind="operation_failure",
                        reason="github_http_rejection",
                        diagnostic=outcome.diagnostic,
                    ),
                )
                posted = _terminal(api, issue, rejected, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                lines.append(f"rejected={request}:{posted.id}:github_http_rejection")
                failed.append(request)
                continue
            if isinstance(outcome, StageHttpIndeterminate):
                indeterminate = _receipt_for(
                    proposal,
                    seal,
                    "indeterminate",
                    operation,
                    ReceiptContent(
                        candidate_body_sha256=candidate.body_sha256,
                        live_body_sha256=sha256_text(live.body or ""),
                        recovery_action=f"itree stage resume {issue.slug} --attempt {proposal.attempt_id}",
                    ),
                    indeterminate_reason=outcome.reason,
                )
                posted = _terminal(api, issue, indeterminate, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                pending.append(request)
                lines.append(f"indeterminate={request}:{posted.id}:{outcome.reason}")
                break
            try:
                GithubIssue.model_validate_json(outcome.response_body)
            except ValidationError:
                indeterminate = _receipt_for(
                    proposal,
                    seal,
                    "indeterminate",
                    operation,
                    ReceiptContent(
                        candidate_body_sha256=candidate.body_sha256,
                        live_body_sha256=sha256_text(live.body or ""),
                        recovery_action=f"itree stage resume {issue.slug} --attempt {proposal.attempt_id}",
                    ),
                    indeterminate_reason="unusable_success_response",
                )
                posted = _terminal(api, issue, indeterminate, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                pending.append(request)
                lines.append(
                    f"indeterminate={request}:{posted.id}:unusable_success_response"
                )
                break
            readback = _require_live_issue(api, issue, expected_node)
            if readback.body != candidate.body:
                indeterminate = _receipt_for(
                    proposal,
                    seal,
                    "indeterminate",
                    operation,
                    ReceiptContent(
                        candidate_body_sha256=candidate.body_sha256,
                        live_body_sha256=sha256_text(live.body or ""),
                        recovery_action=f"itree stage resume {issue.slug} --attempt {proposal.attempt_id}",
                    ),
                    indeterminate_reason="readback_unavailable",
                )
                posted = _terminal(api, issue, indeterminate, seal)
                if not isinstance(posted, IssueComment):
                    return DrainReport(
                        exit_code=5,
                        confirmed=False,
                        lines=("receipt_unavailable=true",),
                    )
                pending.append(request)
                lines.append(
                    f"indeterminate={request}:{posted.id}:readback_unavailable"
                )
                break
            confirmed = _receipt_for(
                proposal,
                seal,
                "confirmed",
                operation,
                ReceiptContent(
                    base_body_sha256=sha256_text(live.body or ""),
                    candidate_body_sha256=candidate.body_sha256,
                    live_body_sha256=candidate.body_sha256,
                    original_sha256=candidate.original_sha256,
                    fence_sha256=_fence_sha(live.body or ""),
                    no_body_write=False,
                    stage_complete=True,
                    artifacts=indexed_entries,
                ),
            )
            posted = _terminal(api, issue, confirmed, seal)
            if not isinstance(posted, IssueComment):
                return DrainReport(
                    exit_code=5, confirmed=False, lines=("receipt_unavailable=true",)
                )
            prefix.append(request)
            last_applied_sha = candidate.body_sha256
            lines.append(f"confirmed={request}:{posted.id}:no_body_write:false")
            live = readback
        high_water = max((record.native.id for record in records), default=0)
        for release in _release_models(records):
            if attempt is not None and release.attempt_id != attempt:
                continue
            prior = receipts.get(release.request_id)
            if prior is not None and prior.status == "session_released":
                lines.append(f"session_released={release.session_id}:replay")
                continue
            active = sessions.get(release.session_id)
            if active is None:
                pending.append(release.request_id)
                lines.append(f"pending={release.request_id}:release_blocked")
                continue
            approved = _external_authorized(
                api,
                _find_record_native(records, release.request_id).native,
                release,
                live,
                active.enrolled_actors,
                True,
            )
            if isinstance(approved, ExternalSubmissionDetail):
                failed.append(release.request_id)
                lines.append(f"rejected={release.request_id}:{approved.reason}")
                continue
            body_is_current = sha256_text(live.body or "") == (
                last_applied_sha or active.entry_body_sha256
            )
            incomplete = tuple(
                proposal.request_id
                for proposal in (*_proposal_models(records), *upload_drain.proposals)
                if receipts.get(proposal.request_id) is None
                or receipts[proposal.request_id].status not in {"confirmed", "rejected"}
            )
            receipts_invalid = any(
                not _receipt_is_current(
                    api,
                    record,
                    ReceiptPayload.model_validate_json(
                        canonical_payload_bytes(record.payload), strict=True
                    ),
                )
                for record in records
                if record.fence == "itree-stage-receipt"
                and isinstance(record.payload, dict)
                and record.payload.get("status") in {"confirmed", "rejected"}
            )
            if pending or incomplete or receipts_invalid or not body_is_current:
                pending.append(release.request_id)
                lines.append(f"pending={release.request_id}:release_blocked")
                continue
            released = _receipt_for(
                release,
                seal,
                "session_released",
                None,
                ReceiptContent(
                    live_body_sha256=sha256_text(live.body or ""),
                    stage_complete=True,
                    high_water_comment_id=release.expected_last_scanned_comment_id,
                ),
            )
            posted = _terminal(api, issue, released, seal)
            if not isinstance(posted, IssueComment):
                return DrainReport(
                    exit_code=5, confirmed=False, lines=("receipt_unavailable=true",)
                )
            lines.append(f"session_released={release.session_id}:{posted.id}")
        remaining = (*pending, *failed)
        return DrainReport(
            exit_code=4 if failed else 5 if pending else 0,
            confirmed=not remaining,
            lines=tuple(f"confirmed_prefix={item}" for item in prefix)
            + tuple(lines)
            + tuple(f"pending_suffix={item}" for item in remaining)
            + (f"high_water_comment_id={high_water}",),
            pending=bool(pending),
        )
    finally:
        _unlock(lock)


def _find_record_native(
    records: tuple[DurableRecord, ...], request: RequestId
) -> DurableRecord:
    return next(
        record
        for record in records
        if isinstance(record.payload, dict)
        and record.payload.get("request_id") == request
    )


EDITABLE_SECTIONS: dict[SectionName, Literal["requirements", "plan", "tasks"]] = {
    SectionName.requirements: "requirements",
    SectionName.plan: "plan",
    SectionName.tasks: "tasks",
}


def _empty_replacement(
    base: ManagedIssueLayout, proposed: ManagedIssueLayout
) -> tuple[SectionReplacement, ...]:
    replacements: list[SectionReplacement] = []
    for section in (SectionName.requirements, SectionName.plan, SectionName.tasks):
        base_text = _section(base, section)
        proposed_text = _section(proposed, section)
        if base_text != proposed_text:
            replacements.append(
                SectionReplacement(
                    section=EDITABLE_SECTIONS[section],
                    base_text=base_text,
                    proposed_text=proposed_text,
                    base_sha256=sha256_text(base_text),
                    proposed_sha256=sha256_text(proposed_text),
                )
            )
    if replacements:
        return tuple(replacements)
    plan = _section(base, SectionName.plan)
    return (
        SectionReplacement(
            section="plan",
            base_text=plan,
            proposed_text=plan,
            base_sha256=sha256_text(plan),
            proposed_sha256=sha256_text(plan),
        ),
    )


def _proposal(
    issue: IssueRef,
    attempt: AttemptId,
    session: SessionId,
    metadata: ScratchMetadata,
    candidate: BodyCandidate,
) -> StageProposal:
    base = _layout(metadata.base_body)
    proposed = _layout(candidate.body)
    if isinstance(base, LegacyIssueLayout):
        requirements = ""
        plan = ""
        tasks = ""
        if isinstance(proposed, ManagedIssueLayout):
            requirements = _section(proposed, SectionName.requirements)
            plan = _section(proposed, SectionName.plan)
            tasks = _section(proposed, SectionName.tasks)
        else:
            assert proposed.body == base.body, (proposed.body, base.body)
        return LegacyIntakeProposal(
            version=1,
            request_kind="legacy_intake",
            issue_node_id=metadata.issue_node_id,
            issue_ref=issue,
            request_id=RequestId("request-" + attempt),
            attempt_id=attempt,
            session_id=session,
            base_body_sha256=metadata.body_sha256,
            legacy_base=base.body,
            requirements=requirements,
            plan=plan,
            tasks=tasks,
        )
    assert isinstance(base, ManagedIssueLayout) and isinstance(
        proposed, ManagedIssueLayout
    )
    return SectionEditProposal(
        version=1,
        request_kind="sections",
        issue_node_id=metadata.issue_node_id,
        issue_ref=issue,
        request_id=RequestId("request-" + attempt),
        attempt_id=attempt,
        session_id=session,
        base_body_sha256=metadata.body_sha256,
        base_body=base.body,
        changes=_empty_replacement(base, proposed),
    )


def apply_stage_candidate(
    issue: IssueRef,
    attempt: AttemptId,
    feature_directory: Path,
    metadata: ScratchMetadata,
    candidate: BodyCandidate,
) -> StageCommandResult:
    api = GithubApi.from_issue_ref(issue)
    live = _require_live_issue(api, issue, metadata.issue_node_id)
    session = SessionId("session-" + attempt)
    session_request = SessionAdmissionRequest(
        version=1,
        request_kind="session_admit",
        issue_node_id=metadata.issue_node_id,
        issue_ref=issue,
        request_id=RequestId("session-admit-" + attempt),
        attempt_id=attempt,
        session_id=session,
        entry_body_sha256=sha256_text(live.body or ""),
        enrolled_actors=(api.get_current_actor().login,),
    )
    records = _all_records(api, issue)
    prior_sessions = [
        record
        for record in records
        if isinstance(record.payload, dict)
        and record.payload.get("request_id") == session_request.request_id
    ]
    if not prior_sessions:
        posted = _post_record(api, issue, "itree-stage-edit", session_request)
        if not isinstance(posted, IssueComment):
            return _pending(issue, attempt)
    proposal = _proposal(issue, attempt, session, metadata, candidate)
    if isinstance(proposal, SectionEditProposal):
        proposal = proposal.model_copy(
            update={"artifacts": collect_artifact_drafts(feature_directory)}
        )
    prior_proposals = [
        record
        for record in records
        if isinstance(record.payload, dict)
        and record.payload.get("request_id") == proposal.request_id
        and record.payload.get("request_kind")
        in {"sections", "task_field", "legacy_intake"}
    ]
    if any(
        canonical_payload_digest(record.payload)
        != canonical_payload_digest(_model_json(proposal))
        for record in prior_proposals
    ):
        if not _writer_enabled():
            return _pending(issue, attempt)
        seal = _writer_runtime_seal(api)
        rejected = _receipt_for(
            proposal,
            seal,
            "rejected",
            _operation(proposal.request_id),
            ReceiptContent(live_body_sha256=sha256_text(live.body or "")),
            "duplicate_payload_conflict",
            MergeConflictDetail(
                kind="merge_conflict",
                section="plan",
                task_id=None,
                field=None,
                competing_request_id=proposal.request_id,
            ),
        )
        posted_rejection = _terminal(api, issue, rejected, seal)
        if not isinstance(posted_rejection, IssueComment):
            return _pending(issue, attempt)
        return StageCommandResult(
            exit_code=4,
            lines=(
                "stage_complete=false",
                f"rejected={proposal.request_id}:{posted_rejection.id}:duplicate_payload_conflict",
                f"pending_suffix={proposal.request_id}",
            ),
        )
    if not prior_proposals:
        posted_proposal = _post_record(api, issue, "itree-stage-edit", proposal)
        if not isinstance(posted_proposal, IssueComment):
            return _pending(issue, attempt)
    if not _writer_enabled():
        return _dispatch_pending(issue, attempt)
    try:
        report = _drain_node(issue, metadata.issue_node_id, attempt)
    except (GithubApiError, StageFailure) as error:
        detail = error.failure.detail if isinstance(error, StageFailure) else str(error)
        code = error.failure.exit_code if isinstance(error, StageFailure) else 3
        return StageCommandResult(
            exit_code=code, lines=("stage_complete=false", detail)
        )
    if report.confirmed:
        cleanup = cleanup_stage_scratch(feature_directory)
        return StageCommandResult(
            exit_code=0,
            lines=(
                f"issue_ref={issue.slug}",
                f"attempt_id={attempt}",
                f"request_id={proposal.request_id}",
                "stage_complete=true",
                f"no_body_write={'true' if candidate.body == live.body else 'false'}",
                f"body_sha256={candidate.body_sha256}",
                f"scratch_cleanup={cleanup}",
                *report.lines,
            ),
        )
    return StageCommandResult(
        exit_code=report.exit_code,
        lines=("stage_complete=false", *report.lines),
    )


def _attempt_disposition(
    issue: IssueRef, attempt: AttemptId
) -> StageCommandResult | None:
    api = GithubApi.from_issue_ref(issue)
    records = _all_records(api, issue)
    live = _require_live_issue(api, issue, None)
    receipts = _receipt_by_request(records)
    sessions = {
        session.session_id: session
        for session in _session_models(records)
        if session.attempt_id == attempt
    }
    proposals = tuple(
        proposal
        for proposal in _proposal_models(records)
        if proposal.attempt_id == attempt
    )
    uploads = tuple(
        manifest
        for manifest in _upload_models(records)
        if manifest.attempt_id == attempt
    )
    if not sessions and not proposals and not uploads:
        return None
    for session in sessions.values():
        receipt = receipts.get(session.request_id)
        if receipt is None or receipt.status != "session_admitted":
            return None
        record = _find_record_native(records, session.request_id)
        if not _receipt_is_current(api, record, receipt):
            return None
    for manifest in uploads:
        state = finalize_upload(manifest, _upload_part_models(records, manifest))
        if not isinstance(state, FinalizedProposal):
            return None
    for proposal in proposals:
        existing = receipts.get(proposal.request_id)
        if existing is None:
            return None
        proposal_session = sessions.get(proposal.session_id)
        if proposal_session is None:
            return None
        approved = _external_authorized(
            api,
            _native_for_proposal(records, proposal.request_id).native,
            proposal,
            live,
            proposal_session.enrolled_actors,
            False,
        )
        if isinstance(approved, ExternalSubmissionDetail):
            return None
        native = next(
            record
            for record in reversed(records)
            if record.fence == "itree-stage-receipt"
            and isinstance(record.payload, dict)
            and record.payload.get("request_id") == proposal.request_id
        )
        if not _receipt_is_current(api, native, existing):
            return None
        if existing.status == "rejected":
            return StageCommandResult(
                exit_code=4,
                lines=(
                    "stage_complete=false",
                    f"rejected={proposal.request_id}:replay",
                ),
            )
        if existing.status != "confirmed":
            return None
    final = next(
        (
            receipt
            for receipt in reversed(tuple(receipts.values()))
            if receipt.status == "confirmed"
        ),
        None,
    )
    if final is None:
        return None
    return StageCommandResult(
        exit_code=0,
        lines=(
            f"issue_ref={issue.slug}",
            f"attempt_id={attempt}",
            "stage_complete=true",
            f"no_body_write={'true' if final.content.no_body_write else 'false'}",
            f"receipt_request_id={final.request_id}",
        ),
    )


def resume_stage(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    disposition = _attempt_disposition(issue, attempt)
    if disposition is not None:
        return disposition
    if not _writer_enabled():
        return _dispatch_pending(issue, attempt)
    try:
        report = _drain_node(issue, None, attempt)
    except (GithubApiError, StageFailure) as error:
        if isinstance(error, StageFailure):
            return StageCommandResult(
                exit_code=error.failure.exit_code,
                lines=(f"reason={error.failure.reason}", error.failure.detail),
            )
        return StageCommandResult(exit_code=3, lines=(str(error),))
    return StageCommandResult(exit_code=report.exit_code, lines=report.lines)


def drain_stage(
    issue: IssueRef, expected_node: IssueNodeId | None
) -> StageCommandResult:
    if not _writer_enabled():
        return _dispatch_pending(issue, AttemptId("drain"))
    try:
        report = _drain_node(issue, expected_node, None)
    except (GithubApiError, StageFailure) as error:
        if isinstance(error, StageFailure):
            return StageCommandResult(
                exit_code=error.failure.exit_code,
                lines=(f"reason={error.failure.reason}", error.failure.detail),
            )
        return StageCommandResult(exit_code=3, lines=(str(error),))
    return StageCommandResult(exit_code=report.exit_code, lines=report.lines)
