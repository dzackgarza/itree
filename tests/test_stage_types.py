from __future__ import annotations

import json
from typing import Literal, assert_type

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from itree.models import (
    AdmissionRejectionDetails,
    CommitEvidence,
    DocumentedTestEvidence,
    EvidenceRef,
    IssueCommentEvidence,
    OperationFailureDetail,
    PullRequestEvidence,
    StageModel,
    TaskParseFailure,
)


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {
                "kind": "issue_comment",
                "repository": "dzackgarza/itree",
                "issue_number": 71,
                "comment_id": 7234802699,
                "comment_node_id": "IC_kwDOTQnYws8AAAABrzpUCw",
                "sha256": "82b30cc3f3acb3f404b6aec38ea11ed172eb87497dacd378306f291e75ff1e82",
                "url": "https://github.com/dzackgarza/itree/issues/71#issuecomment-7234802699",
            },
            IssueCommentEvidence,
        ),
        (
            {
                "kind": "commit",
                "repository": "dzackgarza/itree",
                "sha": "6f704600024a6e1a759b1d94c742262656a47930",
                "url": "https://github.com/dzackgarza/itree/commit/6f704600024a6e1a759b1d94c742262656a47930",
            },
            CommitEvidence,
        ),
        (
            {
                "kind": "pull_request",
                "repository": "dzackgarza/itree",
                "number": 123,
                "head_sha": "6f704600024a6e1a759b1d94c742262656a47930",
                "url": "https://github.com/dzackgarza/itree/pull/123",
            },
            PullRequestEvidence,
        ),
        (
            {
                "kind": "documented_test",
                "command": "uv run --locked pytest tests/test_stage_tasks.py",
                "report": {
                    "repository": "dzackgarza/itree",
                    "issue_number": 71,
                    "attempt_id": "A-T015",
                    "report_id": "v1-box-and-fields",
                },
            },
            DocumentedTestEvidence,
        ),
    ],
)
def test_evidence_union_has_exact_closed_variants(
    payload: dict[str, JsonValue], expected_type: type[StageModel]
) -> None:
    parsed = TypeAdapter(EvidenceRef).validate_python(payload, strict=True)

    assert type(parsed) is expected_type
    assert parsed.model_dump(mode="json") == payload


def test_task_parse_failure_is_one_closed_nine_member_union() -> None:
    expected = Literal[
        "duplicate_id",
        "missing_id",
        "missing_field",
        "malformed_ref",
        "ambiguous_span",
        "unsupported_field",
        "invalid_order",
        "unknown_evidence_kind",
        "unsupported_task_wire",
    ]
    assert_type("unknown_evidence_kind", expected)
    assert_type(assert_type("unsupported_task_wire", expected), TaskParseFailure)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "writer_seal_invalid",
            "repository": "dzackgarza/itree",
            "expected_default_branch": "main",
            "observed_ref": "main",
            "expected_workflow_head": "6f704600024a6e1a759b1d94c742262656a47930",
            "observed_workflow_head": "6f704600024a6e1a759b1d94c742262656a47930",
            "workflow_blob": "workflow-v1",
            "run_id": 101,
            "expected_run_attempt": 1,
            "observed_run_attempt": 2,
            "job_id": 201,
            "job_node_id": "J_writer",
            "failure_dimension": "mismatched_run_attempt",
        },
        {"kind": "external_submission", "actor": None, "reason": "seal_unavailable"},
        {
            "kind": "merge_conflict",
            "section": "plan",
            "task_id": None,
            "field": None,
            "competing_request_id": "A-plan-v1",
        },
    ],
)
def test_admission_details_accept_exact_three_variants(
    payload: dict[str, JsonValue],
) -> None:
    adapter = TypeAdapter(AdmissionRejectionDetails)

    assert (
        adapter.validate_python(payload, strict=True).model_dump(mode="json") == payload
    )
    with pytest.raises(ValidationError):
        adapter.validate_python({**payload, "unexpected": True}, strict=True)


def test_operation_failure_is_not_an_admission_detail() -> None:
    payload = OperationFailureDetail(
        kind="operation_failure", reason="layout_invalid", diagnostic="partial"
    ).model_dump(mode="json")

    with pytest.raises(ValidationError):
        TypeAdapter(AdmissionRejectionDetails).validate_python(payload, strict=True)


def test_admission_details_reject_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(AdmissionRejectionDetails).validate_json(
            json.dumps({"kind": "operation_failure"})
        )
