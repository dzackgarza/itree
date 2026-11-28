from __future__ import annotations

from pathlib import Path

from .models import AttemptId, IssueNodeId, IssueRef
from .stage import BodyCandidate, ScratchMetadata, StageCommandResult


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
